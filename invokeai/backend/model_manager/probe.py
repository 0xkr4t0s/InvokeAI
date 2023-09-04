# Copyright (c) 2023 Lincoln Stein and the InvokeAI Team
"""
Return descriptive information on Stable Diffusion models.

Module for probing a Stable Diffusion model and returning
its base type, model type, format and variant.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable
from picklescan.scanner import scan_file_path

import torch
import safetensors.torch

from .util import read_checkpoint_meta
from .config import (
    ModelType,
    BaseModelType,
    ModelVariantType,
    ModelFormat,
    SchedulerPredictionType,
)
from .util import SilenceWarnings, lora_token_vector_length


class InvalidModelException(Exception):
    """Raised when an invalid model is encountered."""


@dataclass
class ModelProbeInfo(object):
    """Fields describing a probed model."""

    model_type: ModelType
    base_type: BaseModelType
    variant_type: ModelVariantType
    prediction_type: SchedulerPredictionType
    upcast_attention: bool
    format: ModelFormat
    image_size: int


class ModelProbeBase(ABC):
    """Class to probe a checkpoint, safetensors or diffusers folder."""

    @classmethod
    @abstractmethod
    def probe(
        cls,
        model: Path,
        prediction_type_helper: Optional[Callable[[Path], SchedulerPredictionType]] = None,
    ) -> Optional[ModelProbeInfo]:
        """
        Probe model located at path and return ModelProbeInfo object.

        :param model: Path to a model checkpoint or folder.
        :param prediction_type_helper: An optional Callable that takes the model path
        and returns the SchedulerPredictionType.
        """
        pass


class ProbeBase(ABC):
    """Base model for probing checkpoint and diffusers-style models."""

    @abstractmethod
    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType for the model."""
        pass

    def get_variant_type(self) -> ModelVariantType:
        """Return the ModelVariantType for the model."""
        pass

    def get_scheduler_prediction_type(self) -> SchedulerPredictionType:
        """Return the SchedulerPredictionType for the model."""
        pass

    def get_format(self) -> str:
        """Return the format for the model."""
        pass


class ModelProbe(ModelProbeBase):
    """Class to probe a checkpoint, safetensors or diffusers folder."""

    PROBES = {
        "diffusers": {},
        "checkpoint": {},
        "onnx": {},
    }

    CLASS2TYPE = {
        "StableDiffusionPipeline": ModelType.Main,
        "StableDiffusionInpaintPipeline": ModelType.Main,
        "StableDiffusionXLPipeline": ModelType.Main,
        "StableDiffusionXLImg2ImgPipeline": ModelType.Main,
        "AutoencoderKL": ModelType.Vae,
        "ControlNetModel": ModelType.ControlNet,
    }

    @classmethod
    def register_probe(cls, format: ModelFormat, model_type: ModelType, probe_class: ProbeBase):
        """
        Register a probe subclass to use when interrogating a model.

        :param format: The ModelFormat of the model to be probed.
        :param model_type: The ModelType of the model to be probed.
        :param probe_class: The class of the prober (inherits from ProbeBase).
        """
        cls.PROBES[format][model_type] = probe_class

    @classmethod
    def probe(
        cls,
        model: Path,
        prediction_type_helper: Optional[Callable[[Path], SchedulerPredictionType]] = None,
    ) -> Optional[ModelProbeInfo]:
        """Probe model."""
        try:
            model_type = (
                cls.get_model_type_from_folder(model) if model.is_dir() else cls.get_model_type_from_checkpoint(model)
            )
            format_type = "onnx" if model_type == ModelType.ONNX else "diffusers" if model.is_dir() else "checkpoint"

            probe_class = cls.PROBES[format_type].get(model_type)
            if not probe_class:
                return None
            probe = probe_class(model, prediction_type_helper)
            base_type = probe.get_base_type()
            variant_type = probe.get_variant_type()
            prediction_type = probe.get_scheduler_prediction_type()
            format = probe.get_format()
            model_info = ModelProbeInfo(
                model_type=model_type,
                base_type=base_type,
                variant_type=variant_type,
                prediction_type=prediction_type,
                upcast_attention=(
                    base_type == BaseModelType.StableDiffusion2
                    and prediction_type == SchedulerPredictionType.VPrediction
                ),
                format=format,
                image_size=1024
                if (base_type in {BaseModelType.StableDiffusionXL, BaseModelType.StableDiffusionXLRefiner})
                else 768
                if (
                    base_type == BaseModelType.StableDiffusion2
                    and prediction_type == SchedulerPredictionType.VPrediction
                )
                else 512,
            )
        except Exception:
            raise

        return model_info

    @classmethod
    def get_model_type_from_checkpoint(cls, model: Path) -> Optional[ModelType]:
        """
        Scan a checkpoint model and return its ModelType.

        :param model: path to the model checkpoint/safetensors file
        """
        if model.suffix not in (".bin", ".pt", ".ckpt", ".safetensors", ".pth"):
            return None

        if model.name == "learned_embeds.bin":
            return ModelType.TextualInversion

        ckpt = read_checkpoint_meta(model, scan=True)
        ckpt = ckpt.get("state_dict", ckpt)

        for key in ckpt.keys():
            if any(key.startswith(v) for v in {"cond_stage_model.", "first_stage_model.", "model.diffusion_model."}):
                return ModelType.Main
            elif any(key.startswith(v) for v in {"encoder.conv_in", "decoder.conv_in"}):
                return ModelType.Vae
            elif any(key.startswith(v) for v in {"lora_te_", "lora_unet_"}):
                return ModelType.Lora
            elif any(key.endswith(v) for v in {"to_k_lora.up.weight", "to_q_lora.down.weight"}):
                return ModelType.Lora
            elif any(key.startswith(v) for v in {"control_model", "input_blocks"}):
                return ModelType.ControlNet
            elif key in {"emb_params", "string_to_param"}:
                return ModelType.TextualInversion

        else:
            # diffusers-ti
            if len(ckpt) < 10 and all(isinstance(v, torch.Tensor) for v in ckpt.values()):
                return ModelType.TextualInversion

        raise InvalidModelException(f"Unable to determine model type for {model}")

    @classmethod
    def get_model_type_from_folder(cls, model: Path) -> Optional[ModelType]:
        """
        Get the model type of a hugging-face style folder.

        :param model: Path to model folder.
        """
        class_name = None
        if (model / "unet/model.onnx").exists():
            return ModelType.ONNX
        if (model / "learned_embeds.bin").exists():
            return ModelType.TextualInversion
        if (model / "pytorch_lora_weights.bin").exists():
            return ModelType.Lora

        i = model / "model_index.json"
        c = model / "config.json"
        config_path = i if i.exists() else c if c.exists() else None

        if config_path:
            with open(config_path, "r") as file:
                conf = json.load(file)
            class_name = conf["_class_name"]

        if class_name and (type := cls.CLASS2TYPE.get(class_name)):
            return type

        # give up
        raise InvalidModelException(f"Unable to determine model type for {model}")

    @classmethod
    def _scan_and_load_checkpoint(cls, model: Path) -> dict:
        with SilenceWarnings():
            if model.suffix.endswith((".ckpt", ".pt", ".bin")):
                cls._scan_model(model)
                return torch.load(model)
            else:
                return safetensors.torch.load_file(model)

    @classmethod
    def _scan_model(cls, model: Path):
        """
        Scan a model for malicious code.

        :param model: Path to the model to be scanned
        Raises an Exception if unsafe code is found.
        """
        # scan model
        scan_result = scan_file_path(model)
        if scan_result.infected_files != 0:
            raise "The model {model_name} is potentially infected by malware. Aborting import."


# ##################################################3
# Checkpoint probing
# ##################################################3


class CheckpointProbeBase(ProbeBase):
    """Base class for probing checkpoint-style models."""

    def __init__(
        self, model: Path, helper: Optional[Callable[[Path], SchedulerPredictionType]] = None
    ) -> BaseModelType:
        """Initialize the CheckpointProbeBase object."""
        self.checkpoint = ModelProbe._scan_and_load_checkpoint(model)
        self.model = model
        self.helper = helper

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of a checkpoint-style model."""
        pass

    def get_format(self) -> str:
        """Return the format of a checkpoint-style model."""
        return "checkpoint"

    def get_variant_type(self) -> ModelVariantType:
        """Return the ModelVariantType of a checkpoint-style model."""
        model_type = ModelProbe.get_model_type_from_checkpoint(self.model)
        if model_type != ModelType.Main:
            return ModelVariantType.Normal
        state_dict = self.checkpoint.get("state_dict") or self.checkpoint
        in_channels = state_dict["model.diffusion_model.input_blocks.0.0.weight"].shape[1]
        if in_channels == 9:
            return ModelVariantType.Inpaint
        elif in_channels == 5:
            return ModelVariantType.Depth
        elif in_channels == 4:
            return ModelVariantType.Normal
        else:
            raise InvalidModelException(
                f"Cannot determine variant type (in_channels={in_channels}) at {self.checkpoint_path}"
            )


class PipelineCheckpointProbe(CheckpointProbeBase):
    """Probe a checkpoint-style main model."""

    def get_base_type(self) -> BaseModelType:
        """Return the ModelBaseType for the checkpoint-style main model."""
        checkpoint = self.checkpoint
        state_dict = self.checkpoint.get("state_dict") or checkpoint
        key_name = "model.diffusion_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight"
        if key_name in state_dict and state_dict[key_name].shape[-1] == 768:
            return BaseModelType.StableDiffusion1
        if key_name in state_dict and state_dict[key_name].shape[-1] == 1024:
            return BaseModelType.StableDiffusion2
        key_name = "model.diffusion_model.input_blocks.4.1.transformer_blocks.0.attn2.to_k.weight"
        if key_name in state_dict and state_dict[key_name].shape[-1] == 2048:
            return BaseModelType.StableDiffusionXL
        elif key_name in state_dict and state_dict[key_name].shape[-1] == 1280:
            return BaseModelType.StableDiffusionXLRefiner
        else:
            raise InvalidModelException("Cannot determine base type")

    def get_scheduler_prediction_type(self) -> SchedulerPredictionType:
        """Return the SchedulerPredictionType for the checkpoint-style main model."""
        type = self.get_base_type()
        if type == BaseModelType.StableDiffusion1:
            return SchedulerPredictionType.Epsilon
        checkpoint = self.checkpoint
        state_dict = self.checkpoint.get("state_dict") or checkpoint
        key_name = "model.diffusion_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight"
        if key_name in state_dict and state_dict[key_name].shape[-1] == 1024:
            if "global_step" in checkpoint:
                if checkpoint["global_step"] == 220000:
                    return SchedulerPredictionType.Epsilon
                elif checkpoint["global_step"] == 110000:
                    return SchedulerPredictionType.VPrediction
            if (
                self.model and self.helper and not self.model.with_suffix(".yaml").exists()
            ):  # if a .yaml config file exists, then this step not needed
                return self.helper(self.model)
            else:
                return None


class VaeCheckpointProbe(CheckpointProbeBase):
    """Probe a Checkpoint-style VAE model."""

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of the VAE model."""
        # I can't find any standalone 2.X VAEs to test with!
        return BaseModelType.StableDiffusion1


class LoRACheckpointProbe(CheckpointProbeBase):
    """Probe for LoRA Checkpoint Files."""

    def get_format(self) -> str:
        """Return the format of the LoRA."""
        return "lycoris"

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of the LoRA."""
        checkpoint = self.checkpoint
        token_vector_length = lora_token_vector_length(checkpoint)

        if token_vector_length == 768:
            return BaseModelType.StableDiffusion1
        elif token_vector_length == 1024:
            return BaseModelType.StableDiffusion2
        elif token_vector_length == 2048:
            return BaseModelType.StableDiffusionXL
        else:
            raise InvalidModelException(f"Unsupported LoRA type: {self.model}")


class TextualInversionCheckpointProbe(CheckpointProbeBase):
    """TextualInversion checkpoint prober."""

    def get_format(self) -> Optional[str]:
        """Return the format of a TextualInversion emedding."""
        return ModelFormat.EmbeddingFile

    def get_base_type(self) -> BaseModelType:
        """Return BaseModelType of the checkpoint model."""
        checkpoint = self.checkpoint
        if "string_to_token" in checkpoint:
            token_dim = list(checkpoint["string_to_param"].values())[0].shape[-1]
        elif "emb_params" in checkpoint:
            token_dim = checkpoint["emb_params"].shape[-1]
        else:
            token_dim = list(checkpoint.values())[0].shape[0]
        if token_dim == 768:
            return BaseModelType.StableDiffusion1
        elif token_dim == 1024:
            return BaseModelType.StableDiffusion2
        else:
            return None


class ControlNetCheckpointProbe(CheckpointProbeBase):
    """Probe checkpoint-based ControlNet models."""

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of the model."""
        checkpoint = self.checkpoint
        for key_name in (
            "control_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight",
            "input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight",
        ):
            if key_name not in checkpoint:
                continue
            if checkpoint[key_name].shape[-1] == 768:
                return BaseModelType.StableDiffusion1
            elif checkpoint[key_name].shape[-1] == 1024:
                return BaseModelType.StableDiffusion2
            elif self.checkpoint_path and self.helper:
                return self.helper(self.checkpoint_path)
        raise InvalidModelException("Unable to determine base type for {self.checkpoint_path}")


########################################################
# classes for probing folders
#######################################################
class FolderProbeBase(ProbeBase):
    """Class for probing folder-based models."""

    def __init__(self, model: Path, helper: Optional[Callable] = None):  # not used
        """
        Initialize the folder prober.

        :param model: Path to the model to be probed.
        :param helper: Callable for returning the SchedulerPredictionType (unused).
        """
        self.model = model

    def get_variant_type(self) -> ModelVariantType:
        """Return the model's variant type."""
        return ModelVariantType.Normal

    def get_format(self) -> str:
        """Return the model's format."""
        return "diffusers"


class PipelineFolderProbe(FolderProbeBase):
    """Probe a pipeline (main) folder."""

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of a pipeline folder."""
        with open(self.model / "unet" / "config.json", "r") as file:
            unet_conf = json.load(file)
        if unet_conf["cross_attention_dim"] == 768:
            return BaseModelType.StableDiffusion1
        elif unet_conf["cross_attention_dim"] == 1024:
            return BaseModelType.StableDiffusion2
        elif unet_conf["cross_attention_dim"] == 1280:
            return BaseModelType.StableDiffusionXLRefiner
        elif unet_conf["cross_attention_dim"] == 2048:
            return BaseModelType.StableDiffusionXL
        else:
            raise InvalidModelException(f"Unknown base model for {self.folder_path}")

    def get_scheduler_prediction_type(self) -> SchedulerPredictionType:
        """Return the SchedulerPredictionType of a diffusers-style sd-2 model."""
        with open(self.model / "scheduler" / "scheduler_config.json", "r") as file:
            scheduler_conf = json.load(file)
        if scheduler_conf["prediction_type"] == "v_prediction":
            return SchedulerPredictionType.VPrediction
        elif scheduler_conf["prediction_type"] == "epsilon":
            return SchedulerPredictionType.Epsilon
        else:
            return None

    def get_variant_type(self) -> ModelVariantType:
        """Return the ModelVariantType for diffusers-style main models."""
        # This only works for pipelines! Any kind of
        # exception results in our returning the
        # "normal" variant type
        try:
            if self.model:
                conf = self.model.unet.config
            else:
                config_file = self.folder_path / "unet" / "config.json"
                with open(config_file, "r") as file:
                    conf = json.load(file)

            in_channels = conf["in_channels"]
            if in_channels == 9:
                return ModelVariantType.Inpaint
            elif in_channels == 5:
                return ModelVariantType.Depth
            elif in_channels == 4:
                return ModelVariantType.Normal
        except Exception:
            pass
        return ModelVariantType.Normal


class VaeFolderProbe(FolderProbeBase):
    """Probe a diffusers-style VAE model."""

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType for a diffusers-style VAE."""
        config_file = self.folder_path / "config.json"
        if not config_file.exists():
            raise InvalidModelException(f"Cannot determine base type for {self.folder_path}")
        with open(config_file, "r") as file:
            config = json.load(file)
        return (
            BaseModelType.StableDiffusionXL
            if config.get("scaling_factor", 0) == 0.13025 and config.get("sample_size") in [512, 1024]
            else BaseModelType.StableDiffusion1
        )


class TextualInversionFolderProbe(FolderProbeBase):
    """Probe a HuggingFace-style TextualInversion folder."""

    def get_format(self) -> Optional[str]:
        """Return the format of the TextualInversion."""
        return ModelFormat.EmbeddingFolder

    def get_base_type(self) -> BaseModelType:
        """Return the ModelBaseType of the HuggingFace-style Textual Inversion Folder."""
        path = self.model / "learned_embeds.bin"
        if not path.exists():
            raise InvalidModelException(f"This textual inversion folder does not contain a learned_embeds.bin file.")
        return TextualInversionCheckpointProbe(path).get_base_type()


class ONNXFolderProbe(FolderProbeBase):
    """Probe an ONNX-format folder."""

    def get_format(self) -> str:
        """Return the format of the folder (always "onnx")."""
        return "onnx"

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of the ONNX folder."""
        return BaseModelType.StableDiffusion1

    def get_variant_type(self) -> ModelVariantType:
        """Return the ModelVariantType of the ONNX folder."""
        return ModelVariantType.Normal


class ControlNetFolderProbe(FolderProbeBase):
    """Probe a ControlNet model folder."""

    def get_base_type(self) -> BaseModelType:
        """Return the BaseModelType of a ControlNet model folder."""
        config_file = self.model / "config.json"
        if not config_file.exists():
            raise InvalidModelException(f"Cannot determine base type for {self.folder_path}")
        with open(config_file, "r") as file:
            config = json.load(file)
        # no obvious way to distinguish between sd2-base and sd2-768
        dimension = config["cross_attention_dim"]
        base_model = (
            BaseModelType.StableDiffusion1
            if dimension == 768
            else BaseModelType.StableDiffusion2
            if dimension == 1024
            else BaseModelType.StableDiffusionXL
            if dimension == 2048
            else None
        )
        if not base_model:
            raise InvalidModelException(f"Unable to determine model base for {self.folder_path}")
        return base_model


class LoRAFolderProbe(FolderProbeBase):
    """Probe a LoRA model folder."""

    def get_base_type(self) -> BaseModelType:
        """Get the ModelBaseType of a LoRA model folder."""
        model_file = None
        for suffix in ["safetensors", "bin"]:
            base_file = self.model / f"pytorch_lora_weights.{suffix}"
            if base_file.exists():
                model_file = base_file
                break
        if not model_file:
            raise InvalidModelException("Unknown LoRA format encountered")
        return LoRACheckpointProbe(model_file).get_base_type()


############## register probe classes ######
ModelProbe.register_probe("diffusers", ModelType.Main, PipelineFolderProbe)
ModelProbe.register_probe("diffusers", ModelType.Vae, VaeFolderProbe)
ModelProbe.register_probe("diffusers", ModelType.Lora, LoRAFolderProbe)
ModelProbe.register_probe("diffusers", ModelType.TextualInversion, TextualInversionFolderProbe)
ModelProbe.register_probe("diffusers", ModelType.ControlNet, ControlNetFolderProbe)
ModelProbe.register_probe("checkpoint", ModelType.Main, PipelineCheckpointProbe)
ModelProbe.register_probe("checkpoint", ModelType.Vae, VaeCheckpointProbe)
ModelProbe.register_probe("checkpoint", ModelType.Lora, LoRACheckpointProbe)
ModelProbe.register_probe("checkpoint", ModelType.TextualInversion, TextualInversionCheckpointProbe)
ModelProbe.register_probe("checkpoint", ModelType.ControlNet, ControlNetCheckpointProbe)
ModelProbe.register_probe("onnx", ModelType.ONNX, ONNXFolderProbe)
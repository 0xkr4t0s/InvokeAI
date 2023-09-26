"""
Manage a RAM cache of diffusion/transformer models for fast switching.
They are moved between GPU VRAM and CPU RAM as necessary. If the cache
grows larger than a preset maximum, then the least recently used
model will be cleared and (re)loaded from disk when next needed.

The cache returns context manager generators designed to load the
model into the GPU within the context, and unload outside the
context. Use like this:

   cache = ModelCache(max_cache_size=7.5)
   with cache.get_model('runwayml/stable-diffusion-1-5') as SD1,
          cache.get_model('stabilityai/stable-diffusion-2') as SD2:
       do_something_in_GPU(SD1,SD2)


"""

import gc
import hashlib
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union, types

import psutil
import torch

import invokeai.backend.util.logging as logger

from ..util.devices import choose_torch_device
from .models import BaseModelType, ModelBase, ModelType, SubModelType

if choose_torch_device() == torch.device("mps"):
    from torch import mps

# Maximum size of the cache, in gigs
# Default is roughly enough to hold three fp16 diffusers models in RAM simultaneously
DEFAULT_MAX_CACHE_SIZE = 6.0

# amount of GPU memory to hold in reserve for use by generations (GB)
DEFAULT_MAX_VRAM_CACHE_SIZE = 2.75

# actual size of a gig
GIG = 1073741824
MB = 2**20


@dataclass
class CacheStats(object):
    hits: int = 0  # cache hits
    misses: int = 0  # cache misses
    high_watermark: int = 0  # amount of cache used
    in_cache: int = 0  # number of models in cache
    cleared: int = 0  # number of models cleared to make space
    cache_size: int = 0  # total size of cache
    # {submodel_key => size}
    loaded_model_sizes: Dict[str, int] = field(default_factory=dict)


class ModelLocker(object):
    "Forward declaration"
    pass


class ModelCache(object):
    "Forward declaration"
    pass


class _CacheRecord:
    size: int
    model: Any
    cache: ModelCache
    _locks: int

    def __init__(self, cache, model: Any, size: int):
        self.size = size
        self.model = model
        self.cache = cache
        self._locks = 0

    def lock(self):
        self._locks += 1

    def unlock(self):
        self._locks -= 1
        assert self._locks >= 0

    @property
    def locked(self):
        return self._locks > 0

    @property
    def loaded(self):
        if self.model is not None and hasattr(self.model, "device"):
            return self.model.device != self.cache.storage_device
        else:
            return False


class ModelCache(object):
    def __init__(
        self,
        max_cache_size: float = DEFAULT_MAX_CACHE_SIZE,
        max_vram_cache_size: float = DEFAULT_MAX_VRAM_CACHE_SIZE,
        execution_device: torch.device = torch.device("cuda"),
        storage_device: torch.device = torch.device("cpu"),
        precision: torch.dtype = torch.float16,
        sequential_offload: bool = False,
        lazy_offloading: bool = True,
        sha_chunksize: int = 16777216,
        logger: types.ModuleType = logger,
    ):
        """
        :param max_cache_size: Maximum size of the RAM cache [6.0 GB]
        :param execution_device: Torch device to load active model into [torch.device('cuda')]
        :param storage_device: Torch device to save inactive model in [torch.device('cpu')]
        :param precision: Precision for loaded models [torch.float16]
        :param lazy_offloading: Keep model in VRAM until another model needs to be loaded
        :param sequential_offload: Conserve VRAM by loading and unloading each stage of the pipeline sequentially
        :param sha_chunksize: Chunksize to use when calculating sha256 model hash
        """
        self.model_infos: Dict[str, ModelBase] = dict()
        # allow lazy offloading only when vram cache enabled
        self.lazy_offloading = lazy_offloading and max_vram_cache_size > 0
        self.precision: torch.dtype = precision
        self.max_cache_size: float = max_cache_size
        self.max_vram_cache_size: float = max_vram_cache_size
        self.execution_device: torch.device = execution_device
        self.storage_device: torch.device = storage_device
        self.sha_chunksize = sha_chunksize
        self.logger = logger

        # used for stats collection
        self.stats = None

        self._cached_models = dict()
        self._cache_stack = list()

    def get_key(
        self,
        model_path: str,
        base_model: BaseModelType,
        model_type: ModelType,
        submodel_type: Optional[SubModelType] = None,
    ):
        key = f"{model_path}:{base_model}:{model_type}"
        if submodel_type:
            key += f":{submodel_type}"
        return key

    def _get_model_info(
        self,
        model_path: str,
        model_class: Type[ModelBase],
        base_model: BaseModelType,
        model_type: ModelType,
    ):
        model_info_key = self.get_key(
            model_path=model_path,
            base_model=base_model,
            model_type=model_type,
            submodel_type=None,
        )

        if model_info_key not in self.model_infos:
            self.model_infos[model_info_key] = model_class(
                model_path,
                base_model,
                model_type,
            )

        return self.model_infos[model_info_key]

    def log_cache_state(self, prefix=None):
        msg = ""
        msg += f"----- Cache State ({prefix}) -----\n"
        total = 0
        for k, m in self._cached_models.items():
            msg += f"  > {k}: {(m.size/GIG):.2f} GB\n"
            total += m.size
        msg += f"  > CACHE: {(total/GIG):.2f} GB\n"
        msg += f"  > PROCESS: {(psutil.Process().memory_info().rss/GIG):.2f} GB\n"
        vm = psutil.virtual_memory()
        msg += f"  > SYSTEM: {((vm.total-vm.available)/GIG):.2f} GB\n"
        msg += f"-----------------------"

        # self.logger.info(f"MEM_LEAK: ----- Cache State ({prefix}) -----")
        # total = 0
        # for k, m in self._cached_models.items():
        #     self.logger.info(f"MEM_LEAK:  > {k}: {(m.size/GIG):.2f} GB")
        #     total += m.size
        # self.logger.info(f"MEM_LEAK:  > TOTAL: {(total/GIG):.2f} GB")
        # vm = psutil.virtual_memory()

        # self.logger.info(f"MEM_LEAK:  > SYSTEM: {((vm.total-vm.available)/GIG):.2f} GB")
        # self.logger.info(f"MEM_LEAK: -----------------------")
        self.logger.info(f"CACHE_STATE:\n{msg}")

    # TODO: args
    def get_model(
        self,
        model_path: Union[str, Path],
        model_class: Type[ModelBase],
        base_model: BaseModelType,
        model_type: ModelType,
        submodel: Optional[SubModelType] = None,
        gpu_load: bool = True,
    ) -> Any:
        if not isinstance(model_path, Path):
            model_path = Path(model_path)

        if not os.path.exists(model_path):
            raise Exception(f"Model not found: {model_path}")

        model_info = self._get_model_info(
            model_path=model_path,
            model_class=model_class,
            base_model=base_model,
            model_type=model_type,
        )
        key = self.get_key(
            model_path=model_path,
            base_model=base_model,
            model_type=model_type,
            submodel_type=submodel,
        )

        self.logger.info(f"MEM_LEAK: Started get_model(): '{key}'.")
        self.log_cache_state("at start of get_model(...)")

        # TODO: lock for no copies on simultaneous calls?
        cache_entry = self._cached_models.get(key, None)
        if cache_entry is None:
            self.logger.info(
                f"MEM_LEAK: Loading model {model_path}, type"
                f" {base_model.value}:{model_type.value}{':'+submodel.value if submodel else ''}"
            )
            if self.stats:
                self.stats.misses += 1

            # Get the estimated model size before loading.
            predicted_model_size = model_info.get_size(submodel)
            # Make room in the cache to load this model.
            self._make_cache_room(predicted_model_size)

            self.log_cache_state("after _make_cache_room(...)")

            # clean memory to make MemoryUsage() more accurate
            gc.collect()
            model = model_info.get_model(child_type=submodel, torch_dtype=self.precision)

            # Re-calculate model size after loading.
            actual_model_size = model_info.calc_size(model)
            if actual_model_size > 0:
                self.logger.debug(f"CPU RAM used for load: {(actual_model_size/GIG):.2f} GB")
            if abs(actual_model_size - predicted_model_size) > 10 * MB:
                self.logger.warning(
                    f"MEM_LEAK: Predicted model size before load was off by > 10 MB for '{key}'. Predicted:"
                    f" {(predicted_model_size/GIG):.2f} GB, Actual: {(actual_model_size/GIG):.2f} GB."
                )

            cache_entry = _CacheRecord(self, model, actual_model_size)
            self._cached_models[key] = cache_entry
        else:
            if self.stats:
                self.stats.hits += 1

        if self.stats:
            self.stats.cache_size = self.max_cache_size * GIG
            self.stats.high_watermark = max(self.stats.high_watermark, self._cache_size())
            self.stats.in_cache = len(self._cached_models)
            self.stats.loaded_model_sizes[key] = max(self.stats.loaded_model_sizes.get(key, 0), cache_entry.size)

        # Check if the model's self-reported memory usage has changed.
        # TODO(ryand): This logic is a temporary band-aid for the problem of model memory sizes changing in the cache.
        # The main purpose of the current implementation is simply to increase visibility into the problem. The biggest
        # problem with the current implementation is that model memory is only re-calculated on model load (not unload).
        # This means that frequent model switching to could allow many models with under-reported memory sizes to
        # accumulate in the cache.
        cur_model_size = model_info.calc_size(cache_entry.model)
        if cur_model_size != cache_entry.size:
            self.logger.warning(
                f"Detected a change in the size of '{key}' while accessing it from the cache. Cache entry size:"
                f" {(cache_entry.size/GIG):.2f} GB, updated size: {(cur_model_size/GIG):.2f} GB."
            )
            cache_entry.size = cur_model_size

        with suppress(Exception):
            self._cache_stack.remove(key)
        self._cache_stack.append(key)

        self.logger.info(f"MEM_LEAK: Finished get_model(): '{key}'.")
        self.log_cache_state("at end of get_model(...)")

        return self.ModelLocker(self, key, cache_entry.model, gpu_load, cache_entry.size)

    class ModelLocker(object):
        def __init__(self, cache, key, model, gpu_load, size_needed):
            """
            :param cache: The model_cache object
            :param key: The key of the model to lock in GPU
            :param model: The model to lock
            :param gpu_load: True if load into gpu
            :param size_needed: Size of the model to load
            """
            self.gpu_load = gpu_load
            self.cache = cache
            self.key = key
            self.model = model
            self.size_needed = size_needed
            self.cache_entry = self.cache._cached_models[self.key]

        def __enter__(self) -> Any:
            self.cache.logger.info(f"MEM_LEAK: ModelLocker.__enter__ start: '{self.key}'.")
            if not hasattr(self.model, "to"):
                return self.model

            # NOTE that the model has to have the to() method in order for this
            # code to move it into GPU!
            if self.gpu_load:
                self.cache_entry.lock()

                try:
                    if self.cache.lazy_offloading:
                        self.cache._offload_unlocked_models(self.size_needed)

                    if self.model.device != self.cache.execution_device:
                        self.cache.logger.debug(f"Moving {self.key} into {self.cache.execution_device}")
                        with VRAMUsage() as mem:
                            self.model.to(self.cache.execution_device)  # move into GPU
                        self.cache.logger.debug(f"GPU VRAM used for load: {(mem.vram_used/GIG):.2f} GB")

                    self.cache.logger.debug(f"Locking {self.key} in {self.cache.execution_device}")
                    self.cache._print_cuda_stats()

                except Exception:
                    self.cache_entry.unlock()
                    raise

            # TODO: not fully understand
            # in the event that the caller wants the model in RAM, we
            # move it into CPU if it is in GPU and not locked
            elif self.cache_entry.loaded and not self.cache_entry.locked:
                self.model.to(self.cache.storage_device)

            self.cache.logger.info(f"MEM_LEAK: ModelLocker.__enter__ end: '{self.key}'.")
            return self.model

        def __exit__(self, type, value, traceback):
            self.cache.logger.info(f"MEM_LEAK: ModelLocker.__exit__ start: '{self.key}'.")
            if not hasattr(self.model, "to"):
                return

            self.cache_entry.unlock()
            if not self.cache.lazy_offloading:
                self.cache._offload_unlocked_models()
                self.cache._print_cuda_stats()
            self.cache.logger.info(f"MEM_LEAK: ModelLocker.__exit__ end: '{self.key}'.")

    # TODO: should it be called untrack_model?
    def uncache_model(self, cache_id: str):
        with suppress(ValueError):
            self._cache_stack.remove(cache_id)
        self._cached_models.pop(cache_id, None)

    def model_hash(
        self,
        model_path: Union[str, Path],
    ) -> str:
        """
        Given the HF repo id or path to a model on disk, returns a unique
        hash. Works for legacy checkpoint files, HF models on disk, and HF repo IDs

        :param model_path: Path to model file/directory on disk.
        """
        return self._local_model_hash(model_path)

    def cache_size(self) -> float:
        """Return the current size of the cache, in GB."""
        return self._cache_size() / GIG

    def _has_cuda(self) -> bool:
        return self.execution_device.type == "cuda"

    def _print_cuda_stats(self):
        vram = "%4.2fG" % (torch.cuda.memory_allocated() / GIG)
        ram = "%4.2fG" % self.cache_size()

        cached_models = 0
        loaded_models = 0
        locked_models = 0
        for model_info in self._cached_models.values():
            cached_models += 1
            if model_info.loaded:
                loaded_models += 1
            if model_info.locked:
                locked_models += 1

        self.logger.debug(
            f"Current VRAM/RAM usage: {vram}/{ram}; cached_models/loaded_models/locked_models/ ="
            f" {cached_models}/{loaded_models}/{locked_models}"
        )

    def _cache_size(self) -> int:
        return sum([m.size for m in self._cached_models.values()])

    def _make_cache_room(self, model_size):
        # calculate how much memory this model will require
        # multiplier = 2 if self.precision==torch.float32 else 1
        bytes_needed = model_size
        maximum_size = self.max_cache_size * GIG  # stored in GB, convert to bytes
        current_size = self._cache_size()

        if current_size + bytes_needed > maximum_size:
            self.logger.debug(
                f"Max cache size exceeded: {(current_size/GIG):.2f}/{self.max_cache_size:.2f} GB, need an additional"
                f" {(bytes_needed/GIG):.2f} GB"
            )

        self.logger.debug(f"Before unloading: cached_models={len(self._cached_models)}")

        pos = 0
        while current_size + bytes_needed > maximum_size and pos < len(self._cache_stack):
            model_key = self._cache_stack[pos]
            cache_entry = self._cached_models[model_key]

            refs = sys.getrefcount(cache_entry.model)

            # manualy clear local variable references of just finished function calls
            # for some reason python don't want to collect it even by gc.collect() immidiately
            if refs > 2:
                while True:
                    cleared = False
                    for referrer in gc.get_referrers(cache_entry.model):
                        if type(referrer).__name__ == "frame":
                            # RuntimeError: cannot clear an executing frame
                            with suppress(RuntimeError):
                                referrer.clear()
                                cleared = True
                                # break

                    # repeat if referrers changes(due to frame clear), else exit loop
                    if cleared:
                        gc.collect()
                    else:
                        break

            device = cache_entry.model.device if hasattr(cache_entry.model, "device") else None
            self.logger.debug(
                f"Model: {model_key}, locks: {cache_entry._locks}, device: {device}, loaded: {cache_entry.loaded},"
                f" refs: {refs}"
            )

            # 2 refs:
            # 1 from cache_entry
            # 1 from getrefcount function
            # 1 from onnx runtime object
            if not cache_entry.locked and refs <= 3 if "onnx" in model_key else 2:
                self.logger.debug(
                    f"Unloading model {model_key} to free {(model_size/GIG):.2f} GB (-{(cache_entry.size/GIG):.2f} GB)"
                )
                current_size -= cache_entry.size
                if self.stats:
                    self.stats.cleared += 1
                del self._cache_stack[pos]
                del self._cached_models[model_key]
                del cache_entry

            else:
                pos += 1

        gc.collect()
        torch.cuda.empty_cache()
        if choose_torch_device() == torch.device("mps"):
            mps.empty_cache()

        self.logger.debug(f"After unloading: cached_models={len(self._cached_models)}")

    def _offload_unlocked_models(self, size_needed: int = 0):
        reserved = self.max_vram_cache_size * GIG
        vram_in_use = torch.cuda.memory_allocated()
        self.logger.debug(f"{(vram_in_use/GIG):.2f}GB VRAM used for models; max allowed={(reserved/GIG):.2f}GB")
        for model_key, cache_entry in sorted(self._cached_models.items(), key=lambda x: x[1].size):
            if vram_in_use <= reserved:
                break
            if not cache_entry.locked and cache_entry.loaded:
                self.logger.debug(f"Offloading {model_key} from {self.execution_device} into {self.storage_device}")
                with VRAMUsage() as mem:
                    cache_entry.model.to(self.storage_device)
                self.logger.debug(f"GPU VRAM freed: {(mem.vram_used/GIG):.2f} GB")
                vram_in_use += mem.vram_used  # note vram_used is negative
                self.logger.debug(f"{(vram_in_use/GIG):.2f}GB VRAM used for models; max allowed={(reserved/GIG):.2f}GB")

        gc.collect()
        torch.cuda.empty_cache()
        if choose_torch_device() == torch.device("mps"):
            mps.empty_cache()

    def _local_model_hash(self, model_path: Union[str, Path]) -> str:
        sha = hashlib.sha256()
        path = Path(model_path)

        hashpath = path / "checksum.sha256"
        if hashpath.exists() and path.stat().st_mtime <= hashpath.stat().st_mtime:
            with open(hashpath) as f:
                hash = f.read()
            return hash

        self.logger.debug(f"computing hash of model {path.name}")
        for file in list(path.rglob("*.ckpt")) + list(path.rglob("*.safetensors")) + list(path.rglob("*.pth")):
            with open(file, "rb") as f:
                while chunk := f.read(self.sha_chunksize):
                    sha.update(chunk)
        hash = sha.hexdigest()
        with open(hashpath, "w") as f:
            f.write(hash)
        return hash


class VRAMUsage(object):
    def __init__(self):
        self.vram = None
        self.vram_used = 0

    def __enter__(self):
        self.vram = torch.cuda.memory_allocated()
        return self

    def __exit__(self, *args):
        self.vram_used = torch.cuda.memory_allocated() - self.vram

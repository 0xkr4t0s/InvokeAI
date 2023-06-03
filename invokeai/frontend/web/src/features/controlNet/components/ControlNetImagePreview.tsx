import { memo, useCallback, useState } from 'react';
import { ImageDTO } from 'services/api';
import {
  controlNetImageChanged,
  controlNetSelector,
} from '../store/controlNetSlice';
import { useAppDispatch, useAppSelector } from 'app/store/storeHooks';
import { Box } from '@chakra-ui/react';
import IAIDndImage from './parameters/IAISelectableImage';
import { createSelector } from '@reduxjs/toolkit';
import { defaultSelectorOptions } from 'app/store/util/defaultMemoizeOptions';
import { AnimatePresence, motion } from 'framer-motion';
import { IAIImageFallback } from 'common/components/IAIImageFallback';

const selector = createSelector(
  controlNetSelector,
  (controlNet) => {
    const { isProcessingControlImage } = controlNet;
    return { isProcessingControlImage };
  },
  defaultSelectorOptions
);

type Props = {
  controlNetId: string;
  controlImage: ImageDTO | null;
  processedControlImage: ImageDTO | null;
};

const ControlNetImagePreview = (props: Props) => {
  const { controlNetId, controlImage, processedControlImage } = props;
  const dispatch = useAppDispatch();
  const { isProcessingControlImage } = useAppSelector(selector);

  const [shouldShowProcessedImage, setShouldShowProcessedImage] =
    useState(true);

  const handleControlImageChanged = useCallback(
    (controlImage: ImageDTO) => {
      dispatch(controlNetImageChanged({ controlNetId, controlImage }));
    },
    [controlNetId, dispatch]
  );

  const shouldShowProcessedImageBackdrop =
    Number(controlImage?.width) > Number(processedControlImage?.width) ||
    Number(controlImage?.height) > Number(processedControlImage?.height);

  return (
    <Box
      sx={{ position: 'relative', h: 'inherit' }}
      onMouseOver={() => setShouldShowProcessedImage(false)}
      onMouseOut={() => setShouldShowProcessedImage(true)}
    >
      <IAIDndImage
        image={controlImage}
        onDrop={handleControlImageChanged}
        isDropDisabled={Boolean(processedControlImage)}
      />
      <AnimatePresence>
        {controlImage &&
          processedControlImage &&
          shouldShowProcessedImage &&
          !isProcessingControlImage && (
            <motion.div
              initial={{
                opacity: 0,
              }}
              animate={{
                opacity: 1,
                transition: { duration: 0.1 },
              }}
              exit={{
                opacity: 0,
                transition: { duration: 0.1 },
              }}
            >
              <Box
                sx={{
                  position: 'absolute',
                  w: 'full',
                  h: 'full',
                  top: 0,
                  insetInlineStart: 0,
                }}
              >
                {shouldShowProcessedImageBackdrop && (
                  <Box
                    sx={{
                      w: 'full',
                      h: 'full',
                      bg: 'base.900',
                      opacity: 0.7,
                    }}
                  />
                )}
                <Box
                  sx={{
                    position: 'absolute',
                    top: 0,
                    insetInlineStart: 0,
                    w: 'full',
                    h: 'full',
                  }}
                >
                  <IAIDndImage
                    image={processedControlImage}
                    onDrop={handleControlImageChanged}
                    payloadImage={controlImage}
                  />
                </Box>
              </Box>
            </motion.div>
          )}
      </AnimatePresence>
      {isProcessingControlImage && (
        <Box
          sx={{
            position: 'absolute',
            top: 0,
            insetInlineStart: 0,
            w: 'full',
            h: 'full',
          }}
        >
          <IAIImageFallback />
        </Box>
      )}
    </Box>
  );
};

export default memo(ControlNetImagePreview);
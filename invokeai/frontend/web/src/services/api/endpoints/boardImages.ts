import { PatchCollection } from '@reduxjs/toolkit/dist/query/core/buildThunks';
import { OffsetPaginatedResults_ImageDTO_ } from 'services/api/types';
import { ApiFullTagDescription, LIST_TAG, api } from '..';
import { paths } from '../schema';
import { imagesApi } from './images';

type ListBoardImagesArg =
  paths['/api/v1/board_images/{board_id}']['get']['parameters']['path'] &
    paths['/api/v1/board_images/{board_id}']['get']['parameters']['query'];

type AddImageToBoardArg =
  paths['/api/v1/board_images/']['post']['requestBody']['content']['application/json'];

type AddManyImagesToBoardArg =
  paths['/api/v1/board_images/{board_id}/images']['patch']['requestBody']['content']['application/json'];

type RemoveImageFromBoardArg =
  paths['/api/v1/board_images/']['delete']['requestBody']['content']['application/json'];

type RemoveManyBoardImagesArg =
  paths['/api/v1/board_images/images']['post']['requestBody']['content']['application/json'];

export const boardImagesApi = api.injectEndpoints({
  endpoints: (build) => ({
    /**
     * Board Images Queries
     */

    listBoardImages: build.query<
      OffsetPaginatedResults_ImageDTO_,
      ListBoardImagesArg
    >({
      query: ({ board_id, offset, limit }) => ({
        url: `board_images/${board_id}`,
        method: 'DELETE',
        body: { offset, limit },
      }),
    }),

    /**
     * Board Images Mutations
     */

    addBoardImage: build.mutation<void, AddImageToBoardArg>({
      query: ({ board_id, image_name }) => ({
        url: `board_images/`,
        method: 'POST',
        body: { board_id, image_name },
      }),
      invalidatesTags: (result, error, arg) => [
        { type: 'Board', id: arg.board_id },
      ],
      async onQueryStarted(
        { image_name, ...patch },
        { dispatch, queryFulfilled }
      ) {
        const patchResult = dispatch(
          imagesApi.util.updateQueryData('getImageDTO', image_name, (draft) => {
            Object.assign(draft, patch);
          })
        );
        try {
          await queryFulfilled;
        } catch {
          patchResult.undo();
        }
      },
    }),

    addManyBoardImages: build.mutation<
      string[],
      { board_id: string; image_names: string[] }
    >({
      query: ({ board_id, image_names }) => ({
        url: `board_images/${board_id}/images`,
        method: 'PATCH',
        body: image_names,
      }),
      invalidatesTags: (result, error, arg) => {
        const tags: ApiFullTagDescription[] = [
          { type: 'Board', id: arg.board_id },
          { type: 'Board', id: LIST_TAG },
        ];
        return tags;
      },
      async onQueryStarted(
        { image_names, board_id },
        { dispatch, queryFulfilled }
      ) {
        const patches: PatchCollection[] = [];

        image_names.forEach((n) => {
          const patchResult = dispatch(
            imagesApi.util.updateQueryData('getImageDTO', n, (draft) => {
              Object.assign(draft, { board_id });
            })
          );
          patches.push(patchResult);
        });

        try {
          await queryFulfilled;
        } catch {
          patches.forEach((p) => p.undo());
        }
      },
    }),

    deleteBoardImage: build.mutation<void, { image_name: string }>({
      query: (image_name) => ({
        url: `board_images/`,
        method: 'DELETE',
        body: image_name,
      }),
      invalidatesTags: (result, error, arg) => [
        { type: 'Board', id: LIST_TAG },
      ],
      async onQueryStarted({ image_name }, { dispatch, queryFulfilled }) {
        const patchResult = dispatch(
          imagesApi.util.updateQueryData('getImageDTO', image_name, (draft) => {
            Object.assign(draft, { board_id: null });
          })
        );
        try {
          await queryFulfilled;
        } catch {
          patchResult.undo();
        }
      },
    }),

    deleteManyBoardImages: build.mutation<void, { image_names: string[] }>({
      query: ({ image_names }) => ({
        url: `board_images/images`,
        method: 'POST',
        body: image_names,
      }),
      invalidatesTags: (result, error, arg) => [
        { type: 'Board', id: LIST_TAG },
      ],
      async onQueryStarted({ image_names }, { dispatch, queryFulfilled }) {
        const patches: PatchCollection[] = [];

        image_names.forEach((n) => {
          const patchResult = dispatch(
            imagesApi.util.updateQueryData('getImageDTO', n, (draft) => {
              Object.assign(draft, { board_id: null });
            })
          );
          patches.push(patchResult);
        });

        try {
          await queryFulfilled;
        } catch {
          patches.forEach((p) => p.undo());
        }
      },
    }),
  }),
});

export const {
  useAddBoardImageMutation,
  useAddManyBoardImagesMutation,
  useDeleteBoardImageMutation,
  useDeleteManyBoardImagesMutation,
} = boardImagesApi;

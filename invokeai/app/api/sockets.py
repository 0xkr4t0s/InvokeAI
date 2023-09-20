# Copyright (c) 2022 Kyle Schouviller (https://github.com/kyle0654)

from fastapi import FastAPI
from fastapi_events.handlers.local import local_handler
from fastapi_events.typing import Event
from fastapi_socketio import SocketManager

from ..services.events import EventServiceBase


class SocketIO:
    __sio: SocketManager

    def __init__(self, app: FastAPI):
        self.__sio = SocketManager(app=app)

        self.__sio.on("subscribe_queue", handler=self._handle_sub_queue)
        self.__sio.on("unsubscribe_queue", handler=self._handle_unsub_queue)
        local_handler.register(event_name=EventServiceBase.queue_event, _func=self._handle_queue_event)

    async def _handle_queue_event(self, event: Event):
        await self.__sio.emit(
            event=event[1]["event"],
            data=event[1]["data"],
            room=event[1]["data"]["queue_id"],
        )

    async def _handle_sub_queue(self, sid, data, *args, **kwargs):
        if "queue_id" in data:
            self.__sio.enter_room(sid, data["queue_id"])

    async def _handle_unsub_queue(self, sid, data, *args, **kwargs):
        if "queue_id" in data:
            self.__sio.enter_room(sid, data["queue_id"])

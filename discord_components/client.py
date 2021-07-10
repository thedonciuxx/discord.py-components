from discord import (
    Client,
    Message,
    Embed,
    AllowedMentions,
    InvalidArgument,
    User,
    File,
)
from discord.ext.commands import Bot, Context as DContext
from discord.http import Route
from discord.abc import Messageable

from inspect import iscoroutinefunction

from pprint import pprint
from traceback import print_stack

from asyncio import sleep, Task, CancelledError, InvalidStateError
from aiohttp import FormData
from typing import List, Union, Callable, Optional
from json import dumps

from .button import Button
from .select import Select
from .component import Component
from .message import ComponentMessage, PartialComponentMessage
from .interaction import Interaction, InteractionEventType, ButtonEvent, TimeoutEvent


__all__ = ("DiscordComponents",)


__DEBUG = False


if not __DEBUG:
    def empty(*args, **kwargs):
        pass

    pprint = empty
    print_stack = empty
    print = empty


async def run_func(func: Callable, *args, **kwargs):
    if iscoroutinefunction(func):
        await func(*args, **kwargs)
    else:
        func(*args, **kwargs)


class DiscordComponents:
    def __init__(
        self,
        bot: Union[Bot, Client],
        change_discord_methods: bool = True,
        add_listener: bool = True,
    ):
        self.bot = bot

        if change_discord_methods:
            self.change_discord_methods(add_listener=add_listener)

    def change_discord_methods(self, add_listener: bool = True):
        async def send_component_msg_prop(ctxorchannel, *args, **kwargs) -> Message:
            if isinstance(ctxorchannel, DContext):
                return await self.send_component_msg(ctxorchannel.channel, *args, **kwargs)
            else:
                return await self.send_component_msg(ctxorchannel, *args, **kwargs)

        async def edit_component_msg_prop(*args, **kwargs):
            return await self.edit_component_msg(*args, **kwargs)

        async def reply_component_msg_prop(msg, *args, **kwargs):
            return await self.send_component_msg(msg.channel, *args, **kwargs, reference=msg)

        self.bot._button_events = {}

        async def on_socket_response(res):
            if (res["t"] != "INTERACTION_CREATE") or (res["d"]["type"] != 3):
                return

            ctx = self._get_button_event(res)
            for key, value in InteractionEventType.items():
                if value == res["d"]["data"]["component_type"]:
                    event = self.bot._button_events.get(res['d']['message']['id'], None)
                    if event is None:
                        _id = res['d']['data']['custom_id'][:36]
                        event = self.bot._button_events.get(_id, None)
                        if event is not None:
                            ctx.id = _id

                    if event is not None:
                        func = event.get('func', {}).get(res['d']['data']['custom_id'], None)

                        if event.get('auto', True):
                            self.__restart_timeout(res['d']['message']['id'])
                        if func is not None:
                            await run_func(func, ctx)
                        else:
                            self.bot.dispatch(key, ctx)
                    else:
                        self.bot.dispatch(key, ctx)
                    break

        if isinstance(self.bot, Bot) and add_listener:
            self.bot.add_listener(on_socket_response, name="on_socket_response")
        else:
            self.bot.on_socket_response = on_socket_response

        Messageable.send = send_component_msg_prop
        Message.edit = edit_component_msg_prop
        Message.reply = reply_component_msg_prop

    def __make_pop_timeout(self, events_id: str):
        def __pop_timeout(task: Task = None):
            if task is None:
                print('POP NO TASK')
                return
            print('POP', task.get_name())
            print_stack()
            try:
                e = task.exception()
            except CancelledError:
                print('CANCELLED', task.get_name())
                pass
            except InvalidStateError:
                print('INVALID STATE')
                if self.bot._button_events.get(events_id, {}).get('timer', None) == task:
                    self.bot._button_events.pop(events_id, None)
                pass
            else:
                print('ELSE')
                if self.bot._button_events.get(events_id, {}).get('timer', None) == task:
                    self.bot._button_events.pop(events_id, None)
                if e is not None:
                    print('ERROR')
                    raise e
                else:
                    print('NO ERROR')
        return __pop_timeout

    async def __timeout(self, events_id: str, timeout: Union[float, int]):
        if timeout is None:
            return

        print('BEGIN', timeout)

        await sleep(timeout)

        event = self.bot._button_events.get(events_id, None)

        if event is None:
            return

        if 'timeout' in event and event['timeout'] is not None:
            print('TIMEOUT')
            await run_func(event['timeout'], TimeoutEvent(self, event['message']))
            print('TIMEOUT_SUCCESS')
        else:
            await ButtonEvent.disable_buttons(self, event['message'])

    async def __remove_timeout(self, events_id: str, run_first: bool = False):
        if events_id not in self.bot._button_events:
            return

        if run_first:
            event = self.bot._button_events[events_id]

            if event['timer'] is not None and not event['timer'].done():
                event['timer'].cancel()

            if 'timeout' in event and event['timeout'] is not None:
                await run_func(event['timeout'], TimeoutEvent(self, event['message']))
            else:
                await ButtonEvent.disable_buttons(self, event['message'])

            self.__make_pop_timeout(events_id)(event['timer'])

        else:
            timer = self.bot._button_events.get(events_id, {}).get('timer', None)
            if timer is not None and not timer.done():
                timer.cancel()
            self.__make_pop_timeout(events_id)(timer)

    async def remove_timeout(
            self,
            run_first: bool = False,
            *,
            interaction: Interaction = None,
            message: Message = None,
            events_id: str = None
    ):
        if interaction is not None:
            await self.__remove_timeout(interaction.response_id, run_first)
        elif message is not None:
            await self.__remove_timeout(str(message.id), run_first)
        elif events_id is not None:
            await self.__remove_timeout(events_id, run_first)
        else:
            raise ValueError('must set either an interaction, message or events_id')

    def __update_timeout(self, events_id: str, timeout: Union[float, int]):
        if events_id not in self.bot._button_events:
            return

        timer = self.bot._button_events[events_id].get('timer', None)

        if timer is not None and not timer.done():
            timer.cancel()

        timer = self.bot.loop.create_task(self.__timeout(events_id, timeout))
        timer.add_done_callback(self.__make_pop_timeout(events_id))

        self.bot._button_events[events_id]['timer'] = timer
        self.bot._button_events[events_id]['reset'] = timeout

    def update_timeout(
            self,
            timeout: Union[float, int],
            *,
            interaction: Interaction = None,
            message: Message = None,
            events_id: str = None
    ):
        if interaction is not None:
            self.__update_timeout(interaction.response_id, timeout)
        elif message is not None:
            self.__update_timeout(str(message.id), timeout)
        elif events_id is not None:
            self.__update_timeout(events_id, timeout)
        else:
            raise ValueError('must set either an interaction, message or events_id')

    def __restart_timeout(self, events_id: str):
        if events_id not in self.bot._button_events:
            return

        timer = self.bot._button_events[events_id].get('timer', None)

        print('\n\nRESTART')
        print(self.bot._button_events)
        print_stack()

        print(timer)
        if timer is not None and not timer.done():
            timer.cancel()

        if 'reset' in self.bot._button_events[events_id] and self.bot._button_events[events_id]['reset'] is not None:
            timer = self.bot.loop.create_task(self.__timeout(events_id, self.bot._button_events[events_id]['reset']))
            print(' CREATED NEW', timer.get_name())
            timer.add_done_callback(self.__make_pop_timeout(events_id))
            self.bot._button_events[events_id]['timer'] = timer

    def restart_timeout(
            self,
            *,
            interaction: Interaction = None,
            message: Message = None,
            events_id: str = None
    ):
        if interaction is not None:
            self.__restart_timeout(interaction.response_id)
        elif message is not None:
            self.__restart_timeout(str(message.id))
        elif events_id is not None:
            self.__restart_timeout(events_id)
        else:
            raise ValueError('must set either an interaction, message or events_id')

    def _update_button_events(
            self,
            msg: ComponentMessage,
            events_id: str,
            timeout: Union[float, int],
            on_timeout: Callable,
            auto_restart: Optional[bool],
            events: Optional[dict] = None,
            new_c: bool = False,
            comps: List[Component] = None
    ):
        if hasattr(self.bot, '_button_events'):
            if (msg is None or msg.components is None) and comps is None:
                return

            if msg is not None and msg.components is not None:
                comps = msg.components

            if len(sum(comps, [])) == 0:
                timer = self.bot._button_events.get(events_id, {}).get('timer', None)
                self.__make_pop_timeout(events_id)(timer)
                return

            if not isinstance(timeout, (float, int, type(None))):
                raise ValueError('timeout must be a float or integer')

            if not isinstance(on_timeout, (Callable, type(None))):
                raise ValueError('on_timeout must be a function')

            funcs = ComponentMessage._get_obj_button_events(comps) if events is None else events

            if not new_c and timeout is None:
                return

            if len(funcs.keys()) > 0:
                prev = self.bot._button_events.get(events_id, {})
                self.bot._button_events[events_id] = {
                    'func': funcs,
                    'timeout': on_timeout if on_timeout is not None else prev.get('timeout', None),
                    'message': msg,
                    'auto': auto_restart if auto_restart is not None else prev.get('auto', True),
                    'timer': prev.get('timer', None),
                    'reset': timeout if timeout is not None else prev.get('reset', None)
                }
                if timeout is not None:
                    self.restart_timeout(events_id=events_id)

            else:
                timer = self.bot._button_events.get(events_id, {}).get('timer', None)
                self.__make_pop_timeout(events_id)(timer)

    async def send_component_msg(
        self,
        channel: Messageable,
        content: str = "",
        *,
        tts: bool = False,
        embed: Embed = None,
        file: File = None,
        files: List[File] = None,
        mention_author: bool = None,
        allowed_mentions: AllowedMentions = None,
        reference: Message = None,
        components: List[Union[Component, List[Component]]] = None,
        delete_after: float = None,
        timeout: Union[float, int] = None,
        auto_restart: bool = True,
        on_timeout: Callable = None,
        **options,
    ) -> Message:
        state = self.bot._get_state()
        channel = await channel._get_channel()

        if embed is not None:
            embed = embed.to_dict()

        if allowed_mentions is not None:
            if state.allowed_mentions:
                allowed_mentions = state.allowed_mentions.merge(allowed_mentions).to_dict()
            else:
                allowed_mentions = allowed_mentions.to_dict()
        else:
            allowed_mentions = state.allowed_mentions and state.allowed_mentions.to_dict()

        if mention_author is not None:
            allowed_mentions = allowed_mentions or AllowedMentions().to_dict()
            allowed_mentions["replied_user"] = bool(mention_author)

        if reference is not None:
            try:
                reference = reference.to_message_reference_dict()
            except AttributeError:
                raise InvalidArgument(
                    "Reference parameter must be either Message or MessageReference."
                ) from None

        if files:
            if file:
                files.append(file)

            if len(files) > 10:
                raise InvalidArgument("files parameter must be a list of up to 10 elements")
            elif not all(isinstance(file, File) for file in files):
                raise InvalidArgument("files parameter must be a list of File")

        elif file:
            files = [file]

        data = {
            "content": content,
            **self._get_components_json(components),
            **options,
            "embed": embed,
            "allowed_mentions": allowed_mentions,
            "tts": tts,
            "message_reference": reference,
        }

        if files:
            try:
                form = FormData()
                form.add_field(
                    "payload_json", dumps(data, separators=(",", ":"), ensure_ascii=True)
                )
                for index, file in enumerate(files):
                    form.add_field(
                        f"file{index}",
                        file.fp,
                        filename=file.filename,
                        content_type="application/octet-stream",
                    )

                data = await self.bot.http.request(
                    Route("POST", f"/channels/{channel.id}/messages"), data=form, files=files
                )

            finally:
                for f in files:
                    f.close()

        else:
            data = await self.bot.http.request(
                Route("POST", f"/channels/{channel.id}/messages"), json=data
            )

        msg = ComponentMessage(components=components, state=state, channel=channel, data=data)
        self._update_button_events(msg, str(msg.id), timeout, on_timeout, auto_restart, new_c=True)
        if delete_after is not None:
            self.bot.loop.create_task(msg.delete(delay=delete_after))
        return msg

    async def edit_component_msg(
        self,
        message: Message,
        content: str = None,
        *,
        embed: Embed = None,
        allowed_mentions: AllowedMentions = None,
        components: List[Union[Component, List[Component]]] = None,
        timeout: Union[float, int] = None,
        auto_restart: bool = None,
        on_timeout: Callable = None,
        **options,
    ):
        state = self.bot._get_state()
        data = {**self._get_components_json(components), **options}

        new_c = options.pop('new_c', None)
        new_c = components is not None if new_c is None else new_c

        if content is not None:
            data["content"] = content

        if embed is not None:
            embed = embed.to_dict()
            data["embed"] = embed

        if allowed_mentions is not None:
            if state.allowed_mentions:
                allowed_mentions = state.allowed_mentions.merge(allowed_mentions).to_dict()
            else:
                allowed_mentions = allowed_mentions.to_dict()

            data["allowed_mentions"] = allowed_mentions

        data = await self.bot.http.request(
            Route("PATCH", f"/channels/{message.channel.id}/messages/{message.id}"), json=data
        )
        msg = ComponentMessage(components=components, state=state, channel=message.channel, data=data)

        auto = self.bot._button_events.get(str(msg.id), None)
        if auto is not None:
            auto['auto'] = auto.get('auto', True) if auto_restart is None else auto_restart
        else:
            auto = {'auto': True if auto_restart is None else auto_restart}

        if (auto_restart is not None and auto_restart) or (auto_restart is None and auto['auto']) or components is not None:
            self._update_button_events(msg, str(msg.id), timeout, on_timeout, auto_restart, new_c=new_c)

    def _get_components_json(
        self, components: List[Union[Component, List[Component]]] = None
    ) -> dict:
        if not isinstance(components, list) and not components:
            return {}

        for i in range(len(components)):
            if not isinstance(components[i], list):
                components[i] = [components[i]]

        lines = components
        return {
            "components": (
                [
                    {
                        "type": 1,
                        "components": [component.to_dict() for component in components],
                    }
                    for components in lines
                ]
                if lines
                else []
            ),
        }

    def _get_component_type(self, type: int):
        if type == 2:
            return Button
        elif type == 3:
            return Select

    def _structured_raw_data(self, raw_data: dict) -> dict:
        data = {
            "interaction": raw_data["d"]["id"],
            "interaction_token": raw_data["d"]["token"],
            "raw": raw_data,
        }
        raw_data = raw_data["d"]
        state = self.bot._get_state()

        components = []
        if "components" not in raw_data["message"]:
            components = []
        else:
            for i, line in enumerate(raw_data["message"]["components"]):
                components.append([])
                if line["type"] >= 2:
                    components[i].append(self._get_component_type(line["type"]).from_json(line))
                for component in line["components"]:
                    if component["type"] >= 2:
                        components[i].append(
                            self._get_component_type(component["type"]).from_json(component)
                        )

        if raw_data["message"] is not None:
            if "content" in raw_data["message"]:
                data["message"] = ComponentMessage(
                    state=state,
                    channel=self.bot.get_channel(int(raw_data["channel_id"])),
                    data=raw_data["message"],
                    components=components,
                )
            else:
                data["message"] = PartialComponentMessage(
                    channel=self.bot.get_channel(int(raw_data["channel_id"])),
                    components=[],
                    id=int(raw_data["message"]["id"])
                )
        else:
            data["message"] = None

        if "member" in raw_data:
            userData = raw_data["member"]["user"]
        else:
            userData = raw_data["user"]
        data["user"] = User(state=state, data=userData)

        data["component"] = raw_data["data"]
        return data

    def __make_interaction(self, json: dict):
        data = self._structured_raw_data(json)
        rescomponent = []

        if data["message"]:
            for line in data["message"].components:
                for component in line:
                    if isinstance(component, Select):
                        for option in component.options:
                            if option.value in data["values"]:
                                if len(data["values"]) > 1:
                                    rescomponent.append(option)
                                else:
                                    rescomponent = [option]
                                    break
                    else:
                        if component.id == data["component"]["custom_id"]:
                            rescomponent = component
        else:
            rescomponent = Button(label='N/a', id=data["component"]["custom_id"])

        return rescomponent, data

    def _get_interaction(self, json: dict):
        rescomponent, data = self.__make_interaction(json)

        ctx = Interaction(
            bot=self.bot,
            client=self,
            user=data["user"],
            component=rescomponent,
            raw_data=data["raw"],
            message=data["message"],
            is_ephemeral=not bool(data["message"]),
        )
        return ctx

    def _get_button_event(self, json: dict):
        rescomponent, data = self.__make_interaction(json)

        ctx = ButtonEvent(
            bot=self.bot,
            client=self,
            user=data["user"],
            component=rescomponent,
            raw_data=data["raw"],
            message=data["message"],
            is_ephemeral=not bool(data["message"]),
        )
        return ctx

    async def fetch_component_message(self, message: Message) -> ComponentMessage:
        res = await self.bot.http.request(
            Route("GET", f"/channels/{message.channel.id}/messages/{message.id}")
        )
        components = []

        for i in res["components"]:
            components.append([])

            for j in i["components"]:
                components[-1].append(self._get_component_type(j["type"]).from_json(j))

        return ComponentMessage(
            channel=message.channel, state=self.bot._get_state(), data=res, components=components
        )

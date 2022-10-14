"""
Implements Discord chat channel integration.

The Discord API uses a mix of websockets and REST API endpoints.

In order for this integration to work, you need to have your own
discord bot set up via https://discord.com/developers/applications
with the MESSAGE CONTENT toggle switched on, and your bot token
added to `server/conf/secret_settings.py` as your  DISCORD_BOT_TOKEN
"""
import json
import os
from random import random
from io import BytesIO

from autobahn.twisted.websocket import (
    WebSocketClientFactory,
    WebSocketClientProtocol,
    connectWS,
)
from django.conf import settings
from twisted.internet import protocol, reactor, ssl, task
from twisted.web.client import Agent, FileBodyProducer, readBody
from twisted.web.http_headers import Headers

from evennia.server.session import Session
from evennia.utils import class_from_module, get_evennia_version, logger

_AGENT = Agent(reactor)

_BASE_SESSION_CLASS = class_from_module(settings.BASE_SESSION_CLASS)

DISCORD_API_VERSION = 10
# include version number to prevent automatically updating to breaking changes
DISCORD_API_BASE_URL = f"https://discord.com/api/v{DISCORD_API_VERSION}"

DISCORD_USER_AGENT = f"Evennia (https://www.evennia.com, {get_evennia_version(mode='short')})"
DISCORD_BOT_TOKEN = getattr(settings, "DISCORD_BOT_TOKEN", None)
DISCORD_BOT_INTENTS = getattr(settings, "DISCORD_BOT_INTENTS", 105985)

# Discord OP codes, alphabetic
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_HEARTBEAT_ACK = 11
OP_HELLO = 10
OP_IDENTIFY = 2
OP_INVALID_SESSION = 9
OP_RECONNECT = 7
OP_RESUME = 6


class DiscordWebsocketServerFactory(WebSocketClientFactory, protocol.ReconnectingClientFactory):
    """
    A variant of the websocket-factory that auto-reconnects.

    """

    initialDelay = 1
    factor = 1.5
    maxDelay = 60
    gateway = None
    resume_url = None

    def __init__(self, sessionhandler, *args, **kwargs):
        self.uid = kwargs.pop("uid")
        self.sessionhandler = sessionhandler
        self.port = None
        self.bot = None

    def get_gateway_url(self, *args, **kwargs):
        # get the websocket gateway URL from Discord
        d = _AGENT.request(
            b"GET",
            f"{DISCORD_API_BASE_URL}/gateway".encode("utf-8"),
            Headers(
                {
                    "User-Agent": [DISCORD_USER_AGENT],
                    "Authorization": [f"Bot {DISCORD_BOT_TOKEN}"],
                    "Content-Type": ["application/json"],
                }
            ),
            None,
        )

        def cbResponse(response):
            # check status code here to verify it was a successful connection first
            # then schedule a retry if not
            d = readBody(response)
            d.addCallback(self.websocket_init, *args, **kwargs)
            return d

        d.addCallback(cbResponse)

    def websocket_init(self, payload, *args, **kwargs):
        """
        callback for when the URL is gotten
        """
        data = json.loads(str(payload, "utf-8"))
        logger.log_info(f"payload: {data}")
        if url := data.get("url"):
            self.gateway = f"{url}/?v={DISCORD_API_VERSION}&encoding=json".encode("utf-8")
            useragent = kwargs.pop("useragent", DISCORD_USER_AGENT)
            headers = kwargs.pop(
                "headers",
                {
                    "Authorization": [f"Bot {DISCORD_BOT_TOKEN}"],
                    "Content-Type": ["application/json"],
                },
            )

            logger.log_info("Connecting to Discord Gateway...")
            WebSocketClientFactory.__init__(
                self, url, *args, headers=headers, useragent=useragent, **kwargs
            )
            self.start()
        else:
            # TODO: set this up to schedule a retry instead
            logger.log_err("Discord did not return a websocket URL; connection cancelled.")

    def buildProtocol(self, addr):
        """
        Build new instance of protocol

        Args:
            addr (str): Not used, using factory/settings data

        """
        if hasattr(settings, "DISCORD_SESSION_CLASS"):
            protocol_class = class_from_module(
                settings.DISCORD_SESSION_CLASS, fallback=DiscordClient
            )
            protocol = protocol_class()
        else:
            protocol = DiscordClient()

        protocol.factory = self
        protocol.sessionhandler = self.sessionhandler
        return protocol

    def startedConnecting(self, connector):
        """
        Tracks reconnections for debugging.

        Args:
            connector (Connector): Represents the connection.

        """
        logger.log_info("Attempting connection to Discord...")

    def clientConnectionFailed(self, connector, reason):
        """
        Called when Client failed to connect.

        Args:
            connector (Connection): Represents the connection.
            reason (str): The reason for the failure.

        """
        protocol.ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        """
        Called when Client loses connection.

        Args:
            connector (Connection): Represents the connection.
            reason (str): The reason for the failure.

        """
        # what is the purpose of this check??? do i need it
        if not self.bot:
            self.retry(connector)

    def reconnect(self):
        """
        Force a reconnection of the bot protocol. This requires
        de-registering the session and then reattaching a new one.

        """
        self.bot.stopping = True
        self.bot.transport.loseConnection()
        self.sessionhandler.server_disconnect(self.bot)
        if self.resume_url:
            self.url = self.resume_url
        elif self.gateway:
            self.url = self.gateway
        else:
            # we don't know where to reconnect to! start from the beginning
            self.get_gateway_url()
            return
        self.start()

    def start(self):
        "Connect protocol to remote server"

        if not self.gateway:
            # we can't actually start yet
            # get the gateway URL from Discord
            self.get_gateway_url()
        else:
            connectWS(self)


class DiscordClient(WebSocketClientProtocol, _BASE_SESSION_CLASS):
    """
    Implements the grapevine client
    """

    nextHeartbeatCall = None
    pending_heartbeat = False
    heartbeat_interval = None
    last_sequence = 0
    session_id = None

    def __init__(self):
        WebSocketClientProtocol.__init__(self)
        _BASE_SESSION_CLASS.__init__(self)
        self.restart_downtime = None

    #        self.discord_id

    def at_login(self):
        pass

    def onOpen(self):
        """
        Called when connection is established.

        """
        self.restart_downtime = None
        self.restart_task = None

        self.stopping = False
        self.factory.bot = self

        self.init_session("discord", "discord.gg", self.factory.sessionhandler)
        self.uid = int(self.factory.uid)
        self.logged_in = True
        self.sessionhandler.connect(self)

    def onMessage(self, payload, isBinary):
        """
        Callback fired when a complete WebSocket message was received.

        Args:
            payload (bytes): The WebSocket message received.
            isBinary (bool): Flag indicating whether payload is binary or
                             UTF-8 encoded text.

        """
        if isBinary:
            logger.log_info("DISCORD: got a binary payload for some reason")
            return
        data = json.loads(str(payload, "utf-8"))
        if seqid := data.get("s"):
            self.last_sequence = seqid

        # not sure if that error json format is for websockets
        # check for it just in case
        if "errors" in data:
            self.handle_error(data)
            return

        # check for discord gateway API op codes first
        if data["op"] == OP_HELLO:
            self.interval = data["d"]["heartbeat_interval"] / 1000  # convert millisec to seconds
            if self.nextHeartbeatCall:
                self.nextHeartbeatCall.cancel()
            self.nextHeartbeatCall = self.factory._batched_timer.call_later(
                self.interval * random(),
                self.doHeartbeat,
            )
            if self.session_id:
                # we already have a session; try to resume instead
                self.resume()
            else:
                self.identify()
        elif data["op"] == OP_HEARTBEAT_ACK:
            self.pending_heartbeat = False
        elif data["op"] == OP_HEARTBEAT:
            self.doHeartbeat(force=True)
        elif data["op"] == OP_INVALID_SESSION:
            # reconnect
            logger.log_msg("Discord: received 'Invalid Session' opcode. Reconnecting.")
            if data["d"] == False:
                # can't resume, clear existing resume data
                self.session_id = None
                self.factory.resume_url = None
            self.factory.reconnect()
        elif data["op"] == OP_RECONNECT:
            logger.log_msg("Discord: received 'Reconnect' opcode. Reconnecting.")
            self.factory.reconnect()
        elif data["op"] == OP_DISPATCH:
            if data["t"] == "READY":
                self.connection_ready(data["d"])
            else:
                # general message, pass on to data_in
                self.data_in(data=data)

    def onClose(self, wasClean, code=None, reason=None):
        """
        This is executed when the connection is lost for whatever
        reason. it can also be called directly, from the disconnect
        method.

        Args:
            wasClean (bool): ``True`` if the WebSocket was closed cleanly.
            code (int or None): Close status as sent by the WebSocket peer.
            reason (str or None): Close reason as sent by the WebSocket peer.

        """
        if self.nextHeartbeatCall:
            self.nextHeartbeatCall.cancel()
        self.disconnect(reason)
        if code >= 4000:
            logger.log_err(f"Discord connection closed: {reason}")
        else:
            logger.log_info(f"Discord disconnected: {reason}")

    def _send_json(self, data):
        """
        Post JSON data to the websocket

        Args:
            data (dict): content to send.

        """
        return self.sendMessage(json.dumps(data).encode("utf-8"))

    def _post_json(self, url, data, **kwargs):
        """
        Post JSON data to a REST API endpoint

        Args:
            url (str) -
            data (dict) - Content to be sent
        """
        url = f"{DISCORD_API_BASE_URL}/{url}"
        body = FileBodyProducer(BytesIO(json.dumps(data).encode("utf-8")))
        d = _AGENT.request(
            b"POST",
            url.encode("utf-8"),
            Headers(
                {
                    "User-Agent": [DISCORD_USER_AGENT],
                    "Authorization": [f"Bot {DISCORD_BOT_TOKEN}"],
                    "Content-Type": ["application/json"],
                }
            ),
            body,
        )

        def cbResponse(response):
            # check status code here to verify it was a successful connection first
            # then schedule a retry if not
            d = readBody(response)
            d.addCallback(self.post_response)
            return d

        d.addCallback(cbResponse)

    def post_response(self, body, **kwargs):
        """
        Process the response from sending a POST request

        Args:
            body (bytes) - The post response body
        """
        data = json.loads(body)
        if "errors" in data:
            self.handle_error(data)

    def handle_error(self, data, **kwargs):
        """
        General hook for processing errors.

        Args:
            data (dict) - The received error data

        """
        logger.log_err(str(data))

    def resume(self):
        """
        Called after a reconnection to re-identify and replay missed events

        """
        if not self.last_sequence or not self.session_id:
            # we have no known state to resume from, identify normally
            self.identify()

        data = {
            "op": OP_RESUME,
            "d": {
                "token": DISCORD_BOT_TOKEN,
                "session_id": self.session_id,
                "s": self.sequence_id,
            },
        }
        self._send_json(data)

    def disconnect(self, reason=None):
        """
        Generic hook for the engine to call in order to
        disconnect this protocol.

        Args:
            reason (str or None): Motivation for the disconnection.

        """
        self.sessionhandler.disconnect(self)
        self.sendClose(self.CLOSE_STATUS_CODE_NORMAL, reason)

    def identify(self, *args, **kwargs):
        """
        Send Discord authentication. This should be sent once heartbeats begin.

        """
        data = {
            "op": 2,
            "d": {
                "token": DISCORD_BOT_TOKEN,
                "intents": DISCORD_BOT_INTENTS,
                "properties": {
                    "os": os.name,
                    "browser": DISCORD_USER_AGENT,
                    "device": DISCORD_USER_AGENT,
                },
            },
        }
        self._send_json(data)

    def connection_ready(self, data):
        """
        Process READY data for relevant bot info.
        """
        self.factory.resume_url = data["resume_gateway_url"]
        self.session_id = data["session_id"]
        self.discord_id = data["user"]["id"]

    def doHeartbeat(self, *args, **kwargs):
        """
        Send heartbeat to Discord.

        """
        if not self.pending_heartbeat or kwargs.get("force"):
            if self.nextHeartbeatCall:
                self.nextHeartbeatCall.cancel()
            data = {"op": 1, "d": self.last_sequence}
            self._send_json(data)
            self.pending_heartbeat = True
            self.nextHeartbeatCall = self.factory._batched_timer.call_later(
                self.interval,
                self.doHeartbeat,
            )
        else:
            # we didn't get a response since the last heartbeat; reconnect
            self.factory.reconnect()

    def send_channel(self, text, channel_id, **kwargs):
        """
        Send a message from an Evennia channel to a Discord channel.

        Use with session.msg(channel=(message, channel, sender))

        """

        data = {"content": text}
        data.update(kwargs)
        self._post_json(f"channels/{channel_id}/messages", data)

    def send_default(self, *args, **kwargs):
        """
        Ignore other outputfuncs

        """
        pass

    def data_in(self, data, **kwargs):
        """

        Send data grapevine -> Evennia
        Keyword Args:
            data (dict): Converted json data.

        """
        action_type = data.get("t", "UNKNOWN")
        logger.log_msg(f"DISCORD: Received an action of type {action_type}.")

        if action_type == "MESSAGE_CREATE":
            logger.log_msg(str(data))
            data = data["d"]
            if data["author"]["id"] == self.discord_id:
                return
            message = data["content"]
            channel_id = data["channel_id"]
            keywords = {"channel_id": channel_id}
            if "guild_id" in data:
                # channel message
                keywords["type"] = "channel"
                author = data["member"]["nick"] or data["author"]["username"]
                author_id = data["author"]["id"]
                keywords["sender"] = (author_id, author)
                keywords["guild_id"] = data["guild_id"]

            else:
                # direct message
                keywords["type"] = "direct"
                author = data["author"]["username"]
                author_id = data["author"]["id"]
                keywords["sender"] = (author_id, author)

            self.sessionhandler.data_in(self, bot_data_in=(message, keywords))

        elif action_type in ("GUILD_CREATE", "GUILD_UPDATE"):
            data = data["d"]
            keywords = {"type": "guild", "guild_id": data["id"], "guild_name": data["name"]}
            keywords["channels"] = {
                chan["id"]: {"name": chan["name"], "guild": data["name"]}
                for chan in data["channels"]
                if chan["type"] == 0
            }
            self.sessionhandler.data_in(self, bot_data_in=("", keywords))

        elif "DELETE" in action_type:
            # deletes should probably be handled separately to check for channel removal
            # for now, just ignore
            pass

        else:
            # send all the data on to the bot as-is for optional bot-side handling
            keywords = {"type": action_type}
            keywords.update(data["d"])
            self.sessionhandler.data_in(self, bot_data_in=("", keywords))

# -*- coding: utf-8 -*-

# Copyright (c) 2016 Ericsson AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from calvin.actor.actor import Actor, manage, condition, stateguard, calvinsys

from calvin.utilities.calvinlogger import get_logger

_log = get_logger(__name__)


class MQTTDynamicSubscriber(Actor):
    """
    Subscribe to various topics using a various number of MQTT brokers

    Arguments:
    settings is a dictionary with optional arguments:
        {
            "tls": {
                "ca_certs": <ca certs>, "certfile": <certfile>, "keyfile": <keyfile>,
                "tls_version": <tls version>, "ciphers": <ciphers>
            },
            "auth": { "username": <username "password": <password> },
            "will": { "topic": <topic>, "payload": <payload> },
            "transport": <tcp or websocket>,
            "clean_session": <true|false>
        }

    Input:
        client_id : MQTT client ID
        uri : MQTT broker URI (format: schema://host:port)
        cmd : command keyword ('subscribe'|'unsubscribe')
        topic : topic to subscribe to
        qos : MQTT qos
    Output:
        message : dictionary {"topic": <topic>, "payload": <payload>, "client_id": <client id>}
    """

    @manage(['settings', 'mqtt_dict', 'queue'])
    def init(self, settings):
        if not settings:
            settings = {}
        self.settings = settings
        self.mqtt_dict = {}
        self.queue = []

    """
    Read all available MQTT clients for messages and store them in a FIFO queue
    The reader will only read the first message in the queue.

    @note The rest of the messages are expected to be read at the next readings
    """

    @stateguard(lambda self: self.queue or
                any(calvinsys.can_read(mqtt) for mqtt in self.mqtt_dict.itervalues()))
    @condition(action_output=['message'])
    def read_message(self):
        message = ""
        for (client_id, mqtt) in self.mqtt_dict.iteritems():
            if (calvinsys.can_read(mqtt)):
                message = calvinsys.read(mqtt)
                # add client id to the message
                message["client_id"] = client_id
                self.queue.append(message)
        if (self.queue):
            message = self.queue.pop(0)
        return (message,)

    """
    Update MQTT subscribed topics for specific MQTT client
    """

    @condition(action_input=['client_id', 'uri', 'cmd', 'topic', 'qos'])
    def update_topic(self, client_id, uri, cmd, topic, qos):
        if (topic is None):
            _log.warning("Topic is missing!")
            return

        if (not client_id in self.mqtt_dict.keys()):
            self.mqtt_dict[client_id] = calvinsys.open(self, "mqtt.subscribe",
                                                       client_id=client_id,
                                                       topics=[topic],
                                                       uri=uri,
                                                       qos=qos,
                                                       **self.settings)
        calvinsys.write(self.mqtt_dict[client_id],
                        {"cmd": cmd, "topic":topic, "qos":qos})

    action_priority = (update_topic, read_message)
    requires = ['mqtt.subscribe']

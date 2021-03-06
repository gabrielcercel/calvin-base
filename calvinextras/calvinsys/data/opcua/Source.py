# -*- coding: utf-8 -*-

# Copyright (c) 2017 Ericsson AB
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

import time
from collections import deque
from socket import error as socket_error
import opcua
from calvin.runtime.south.async import async, threads
from calvin.runtime.south.calvinsys import base_calvinsys_object
from calvin.utilities.calvinlogger import get_logger
from calvin.utilities.calvinlogger import add_logging_handler

_log = get_logger(__name__)

# Necessary in order to get logs from opcua-package
add_logging_handler("opcua")


class FatalException(Exception):
    pass

class NonFatalException(Exception):
    pass

class ChangeHandler(object):
    def __init__(self, notify, node_to_tag):
        super(ChangeHandler, self).__init__()
        self.notify = notify
        self.node_to_tag = node_to_tag

    def datachange_notification(self, node, val, data):
        def data_value_to_struct(data_value):
            import datetime
            sourcets = serverts = None
            try:
                sourcets = (data_value.SourceTimestamp - datetime.datetime(1970, 1, 1)).total_seconds()
                serverts = (data_value.ServerTimestamp - datetime.datetime(1970, 1, 1)).total_seconds()
            except Exception:
                if not serverts:
                    serverts = time.time()
                if not sourcets :
                    sourcets = serverts
            return {
                # "type": data_value.Value.VariantType.name,
                "value": val,
                "status": data_value.StatusCode.value,
                "sourcets": sourcets,
                "serverts": serverts
            }

        def node_id_as_string(node):
            nodeid = str(node.nodeid)
            # nodeid is of the form (ns=X;s=XXX), remove parentheses
            nodeid = nodeid[nodeid.index('(')+1:nodeid.rindex(')')]
            return nodeid

        if not self.node_to_tag.get(node):
            _log.info("Skipping unknown node %s : %s" % (node, str(val)))
            return

        value = data_value_to_struct(data.monitored_item.Value)
        value['tag'] = self.node_to_tag[node]
        async.call_from_thread(self.notify, value)

    def event_notification(self, event):
        _log.info("Received event: {}".format(event))

    def status_change_notification(self, status):
        # I have no idea what this is
        _log.info("OPCUA Status change: {}".format(status))

class Source(base_calvinsys_object.BaseCalvinsysObject):
    """
    Connect to OPCUA server and subscribe to configured parameters
    """

    init_schema = {
        "type": "object",
        "properties": {
            "endpoint": {
                "description": "OPC-UA endpoint uri (opc.tcp://<ip:port>)",
                "type": "string"
            },
            "namespace": {
                "description": "Namespace of parameters - it is assumed all parameters are in the same namespace",
                "type": "string"
            },
            "paramconfig": {
                "description": "Parameter descriptions of the form <tag>:{\"info\": <description>, \"address\": <address> }",
                "type": "object"
            },
            "timeout": {
                "description": "If no data within timeout seconds, check connection, possibly reconnect",
                "type": "integer",
                "minimum": 1
            },
            "monitoring_interval": {
                "description": "Rate, in ms, which the server checks data source for changes",
                "type": "integer",
                "minimum": 100

            },
            "reconnect_interval": {
                "description": "Rate, in seconds, which to attempt reconnect on connection failures",
                "type": "integer",
                "minimum": 10
            },
            "logging_interval": {
                "description": "Log some statistics at given intervals",
                "type": "number",
                "minimum": 30
            }
        },
        "required": ["endpoint", "namespace", "timeout", "monitoring_interval", "paramconfig"],
        "description": ""
    }

    can_read_schema = {
        "description": "Returns True if there is data to read, otherwise False",
        "type": "boolean"
    }

    read_schema = {
        "description": "Get next changed parameter",
        "type": "array",
        "items": {
            "type": "object"
        }
    }


    def init(self, endpoint, paramconfig, namespace, timeout, monitoring_interval, logging_interval=None, reconnect_interval=60, tags=None):
        #pylint: disable=W0201
        self.values = deque()
        self.running = False
        self.data_changed = False
        self.client = None
        self.watchdog = None
        self.retry_connection = None
        self.subscription = None
        self.stat_logger = None

        if logging_interval:
            def show_stats():
                incoming = self.stats["incoming"]
                speed_in = self.stats["current_in"]/logging_interval
                outgoing = self.stats["outgoing"]
                speed_out = self.stats["current_out"]/logging_interval
                _log.info("{} - incoming {} ({}/sec), sent: {} ({}/sec)".format(endpoint, incoming, speed_in, outgoing, speed_out))
                self.stats["current_in"] = 0
                self.stats["current_out"] = 0
                if self.stat_logger:
                    self.stat_logger.reset()

            self.stats = {"incoming": 0, "outgoing": 0, "current_in": 0, "current_out": 0}
            self.stat_logger = async.DelayedCall(logging_interval, show_stats)

        def notify_change(value):
            if not self.running:
                # In the process of closing down, just drop it.
                return
            self.data_changed = True
            value["calvints"] = time.time()
            value["id"] = "ns=" + namespace + ";" + paramconfig[value["tag"]]["address"]
            self.values.append(value)
            if self.stat_logger:
                self.stats["incoming"] += 1
                self.stats["current_in"] += 1
            self.scheduler_wakeup()

        def watchdog(parameter):
            def check_connection():
                try:
                    _log.info("Watchdog triggered - checking connection")
                    node = self.client.get_node(parameter)
                    node.get_data_value()
                except Exception as e:
                    _log.info("Check connection error: {}".format(e))
                    return True
                return False

            def result(failure=None):
                if not failure:
                    _log.info("Connection OK, resetting watchdog")
                    # We have contact with opcua server, reset watchdog, continue as before
                    self.watchdog = async.DelayedCall(timeout, watchdog, parameter)
                    # Alert the scheduler
                    self.scheduler_wakeup()
                else:
                    # Error getting value from server, reset connection
                    _log.warning("Connection check failed, resetting connection")
                    setup()

            if not self.running:
                _log.info("Client not running, shutting down watchdog")
                return

            if not self.data_changed:
                _log.info("Warning: No change in %d seconds; checking connection" % (timeout,))
                # check connection here, possibly reconnect
                check_connection = threads.defer_to_thread(check_connection)
                check_connection.addBoth(result)
            else :
                self.data_changed = False
                self.watchdog = async.DelayedCall(timeout, watchdog, parameter)

        def _connect_and_subscribe(client):

            try:
                client.connect()
            except opcua.ua.uaerrors._auto.BadTooManySessions:
                # Not really fatal, need to retry in a while
                # TODO: How long is a while?
                raise NonFatalException("Too many sessions")
            except socket_error as e:
                # May or may not be fatal, retry in a while
                # TODO: How long is a while?
                raise NonFatalException("Socket error %s" % (e,))
            except Exception as e:
                raise FatalException("Failed to connect: %s" % (e))

            try:
                # get namespace index for namespace
                namespace_idx = client.get_namespace_index(namespace)
            except ValueError:
                # @TODO Inform user?
                _log.error("Server has no namspace %s" % (namespace,))
                raise FatalException("Server has no namespace %s" % (namespace,))

            # Build node-ids for parameters
            parameters = {"ns={};{}".format(namespace_idx, str(desc["address"])) : str(tag) for tag, desc in paramconfig.items()}

            # collect nodes for all parameters
            try:
                node_to_tag = { client.get_node(n) : parameters[n] for n in parameters }
            except Exception as e:
                raise FatalException("Failed to get nodes: %s" % (e,))

            try:
                # create subscription
                subscription = client.create_subscription(monitoring_interval, ChangeHandler(notify_change, node_to_tag))
                # subscribe
                subscription.subscribe_data_change(node_to_tag.keys())
                # Not all servers support this, will give "BadNodeIdUnknown" during setup
                # TODO: When is this of interest?
                # subscription.subscribe_events()
            except Exception as e:
                raise FatalException("Failed to setup subscription %s" % (e,))

            return client, parameters.keys()[0], subscription

        def connect_and_subscribe():
            if self.client:
                try:
                    # if already connected, unsubscribe and disconnect
                    _log.info("Disconnecting due to reconnect")
                    self.client.disconnect()
                except Exception as e:
                    _log.info("Error during disconnect from %s: %s" % (endpoint, e))

            try:
                error = False
                client = opcua.Client(endpoint)
                return _connect_and_subscribe(client)
            except FatalException as e:
                error = True
                _log.error("Fatal error during connection to %s: %s" % (endpoint, e))
            except NonFatalException as e:
                error = True
                _log.warning("Nonfatal error during connection to %s: %s" % (endpoint, e))
                _log.info("Connection retry in {} seconds".format(reconnect_interval))
                async.DelayedCall(reconnect_interval, setup)
            except Exception as e:
                # For unhandled exceptions, we assume them to be fatal
                error = True
                _log.error("Unhandled exception during connection to %s: %s" % (endpoint, e))
            finally:
                if error and client:
                    _log.error("Error occured - disconnecting")
                    client.disconnect()

        def setup_done((client, parameter, subscription)):
            self.client = client
            self.subscription = subscription
            self.running = True
            self.watchdog = async.DelayedCall(timeout, watchdog, parameter)
            # All set - alert the scheduler
            _log.info("Setup complete")
            self.scheduler_wakeup()

        def setup_failed(failure):
            _log.info("Setup of connection to {} failed - retrying in {}".format(endpoint, reconnect_interval))
            async.DelayedCall(reconnect_interval, setup)
            self.scheduler_wakeup()

        def setup():
            _log.info("(Re-)setting connection")
            defer = threads.defer_to_thread(connect_and_subscribe)
            defer.addCallback(setup_done)
            defer.addErrback(setup_failed)

        setup()

        # can_read <-
        # unsubscribe
        # reconnect


    def can_read(self):
        return self.running and len(self.values) > 0

    def read(self):
        values = list(self.values)
        self.values.clear()
        if self.stat_logger:
            self.stats["outgoing"] += len(values)
            self.stats["current_out"] += len(values)
        return values

    def close(self):
        self.running = False
        if self.stat_logger:
            self.stat_logger.cancel()
            self.statlogger = None
        if self.watchdog:
            self.watchdog.cancel()
            self.watchdog = None
        if self.retry_connection:
            self.retry_connection.cancel()
            self.retry_connection = None

        if self.client:
            self.client.disconnect()


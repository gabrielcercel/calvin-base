# -*- coding: utf-8 -*-

# Copyright (c) 2015 Ericsson AB
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

# encoding: utf-8

from calvin.actor.actor import Actor, ActionResult, manage, condition, stateguard
from calvin.runtime.north.calvin_token import EOSToken, ExceptionToken
from calvin.actorstore.systemactors.json.SetValue import SetValue
from copy import deepcopy

class SetValues(SetValue):

    """
    Modify a container (list or dictionary)

    If container is a list then the key must be an integer index (zero-based), or a list of indices if for nested lists.
    If container is a dictionary the key must be a string or list of (string) keys for nested dictionaries.
    It is OK to make a key list of mixed strings and integers if the container comprises nested dictionaries and lists.
    Produce an ExceptionToken if mapping between key and (sub-)container is incorrect, or if a  integer index is out of range.
    N.B. This actor will incur a performance penalty from a deep copy operation. Use wisely.

    Inputs:
      container: a list or dictionary
      keys: A list of indices (integer), keys (string), or a (possibly mixed) list for nested containers
      values: A list of values to set for the corresponding keys
    Outputs:
      container:  a modified container
    """



    @condition(['container', 'keys', 'values'], ['container'])
    def set_values(self, data, keys, values):
        container = deepcopy(data)
        for key, value in zip(keys, values):
            if type(container) is ExceptionToken:
                break
            container = self._set_value(container, key, value)
        return ActionResult(production=(container, ))

    action_priority = (set_values, )

    test_args = []
    test_kwargs = {}

    test_set = [
        {
            'in': {'container': [{'a':1}], 'keys':[['a']], 'values':[[42]]},
            'out': {'container': [{'a':42}]},
        },
        {
            'in': {'container': [[1,2,3]], 'keys':[[1]], 'values':[[42]]},
            'out': {'container': [[1,42,3]]},
        },
        {
            'in': {'container': [[1,{'a':2},3]], 'keys':[[[1, 'a'], 2]], 'values':[[42, 43]]},
            'out': {'container': [[1,{'a':42},43]]},
        },
        {
            'in': {'container': [{'a':[1, 2, 3]}], 'keys':[[['a', 1]]], 'values':[[42]]},
            'out': {'container': [{'a':[1, 42, 3]}]},
        },
        # # Error conditions
        {
            'in': {'container': [[1,2,3]], 'keys':[['a']], 'values':[[42]]},
            'out': {'container': ['Exception']},
        },
        {
            'in': {'container': [{'a':1}], 'keys':[[1]], 'values':[[42]]},
            'out': {'container': ['Exception']},
        },
        {
            'in': {'container': [[1,{'a':2},3]], 'keys':[[0, [1, 2]]], 'values':[[42, 43]]},
            'out': {'container': ['Exception']},
        },

    ]
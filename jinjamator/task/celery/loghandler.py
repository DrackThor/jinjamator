# Copyright 2019 Wilhelm Putz

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import json
from copy import deepcopy
from json_log_formatter import JSONFormatter
from datetime import datetime


def _redact_password(obj, is_password=False):
    if isinstance(obj, str):
        if is_password:
            obj = "<redacted>"
        return obj
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            obj[index] = _redact_password(item)
        return obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if "pass" in k and isinstance(v, str):
                obj[k] = _redact_password(v, True)
            else:
                obj[k] = _redact_password(v)
        return obj
    return obj


class NullCelery(object):
    def update_state(self, **kwargs):
        pass


class CeleryLogHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self._celery_state = "PROGRESS"
        self._celery_task = NullCelery()
        self._celery_meta_template = {}
        self._q = []

    @property
    def contents(self):
        return self._q

    def set_celery_task(self, task):
        self._celery_task = task

    def set_celery_meta_template(self, meta):
        self._celery_meta_template = {}

    def set_celery_state(self, state):
        self._celery_state = state

    def emit(self, record):
        meta = deepcopy(self._celery_meta_template)
        self._q.append(json.loads(self.format(record)))
        meta["log"] = self._q
        if record.levelno == logging.ERROR:
            self._celery_state = "ERROR"
        self._celery_task.update_state(state=self._celery_state, meta=meta)
        self._q = []


class CeleryLogFormatter(JSONFormatter):
    def set_jinjamator_task(self, task):
        self._task = task

    def set_root_task_path(self, path):
        self._root_task = path

    def json_record(self, message, extra, record):

        if "time" not in extra:
            extra["time"] = datetime.utcnow()

        if record.exc_info:
            message = message + str(self.formatException(record.exc_info))

        retval = {
            str(extra["time"]): {
                "root_task": self._root_task,
                "parent_tasklet": self._task._parent_tasklet,
                "parent_task_id": self._task._parent_task_id,
                "current_task": self._task.task_base_dir,
                "current_tasklet": self._task._current_tasklet,
                "current_task_id": id(self._task),
                "message": message,
                "level": logging.getLevelName(record.levelno),
                "configuration": _redact_password(
                    deepcopy(self._task.configuration._data)
                ),
                "stdout": self._task._stdout.getvalue(),
            }
        }

        if record.exc_info:
            retval["exc_info"] = self.formatException(record.exc_info)
        return retval

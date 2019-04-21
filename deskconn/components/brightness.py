#
# Copyright (C) 2018-2019  Omer Akram
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#

import math
import os
import time

from autobahn.twisted import wamp
from twisted.internet import inotify
from twisted.python import filepath
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet import threads

DRIVER_ROOT = '/sys/class/backlight/intel_backlight/'
BRIGHTNESS_CONFIG_FILE = os.path.join(DRIVER_ROOT, 'brightness')
BRIGHTNESS_MAX_REFERENCE_FILE = os.path.join(DRIVER_ROOT, 'max_brightness')
BRIGHTNESS_STEP = 25
BRIGHTNESS_MIN = 1


class _BrightnessControl:
    def __init__(self):
        super().__init__()
        with open(BRIGHTNESS_MAX_REFERENCE_FILE) as file:
            self._brightness_max = int(file.read().strip())
        self.change_in_progress = False

    @staticmethod
    def has_backlight():
        return os.path.exists(BRIGHTNESS_MAX_REFERENCE_FILE)

    @property
    def max_brightness(self):
        return self._brightness_max

    def validate_and_sanitize_brightness_value(self, value):
        assert (isinstance(value, int) or isinstance(value, float)), 'brightness must be either int or float'

        if value < 1:
            return 1
        if value > 100:
            return 100
        return value

    def percent_to_internal(self, percent):
        validated = self.validate_and_sanitize_brightness_value(percent)
        return int((validated / 100) * self.max_brightness)

    @property
    def brightness_current(self):
        with open(BRIGHTNESS_CONFIG_FILE) as config_file:
            return int(config_file.read().strip())

    def write_brightness_value(self, value):
        with open(BRIGHTNESS_CONFIG_FILE, 'w') as config_file:
            config_file.write(str(value))

    def get_current_brightness_percentage(self, current_brightness_raw=0):
        # Calculate brightness percentage from provided "raw" value
        if current_brightness_raw > 0:
            return int((current_brightness_raw / self.max_brightness) * 100)
        # Seems we need to read from the backend
        return int((self.brightness_current / self.max_brightness) * 100)

    def set_brightness(self, percent):
        brightness_requested = self.percent_to_internal(percent)
        # Abort any in progress change
        self.change_in_progress = False

        brightness = self.brightness_current

        self.change_in_progress = True
        if brightness_requested > brightness:
            decimal_steps, full_steps = math.modf((brightness_requested - brightness) / BRIGHTNESS_STEP)
            for i in range(int(full_steps)):
                if not self.change_in_progress:
                    break
                brightness += BRIGHTNESS_STEP
                self.write_brightness_value(brightness)
                time.sleep(0.02)
            if self.change_in_progress:
                brightness += int(decimal_steps * BRIGHTNESS_STEP)
                self.write_brightness_value(brightness)
        else:
            decimal_steps, full_steps = math.modf((brightness - brightness_requested) / BRIGHTNESS_STEP)
            for i in range(int(full_steps)):
                if not self.change_in_progress:
                    break
                brightness -= BRIGHTNESS_STEP
                self.write_brightness_value(brightness)
                time.sleep(0.02)
            if self.change_in_progress:
                brightness -= int(decimal_steps * BRIGHTNESS_STEP)
                self.write_brightness_value(brightness)

        # Ensure brightness is correct at the end
        if brightness != brightness_requested:
            self.write_brightness_value(brightness_requested)

        self.change_in_progress = False


class ScreenBrightnessComponent(wamp.ApplicationSession):
    def __init__(self, config=None):
        super().__init__(config)
        if not _BrightnessControl.has_backlight():
            return

        self.controller = _BrightnessControl()
        self.notifier = inotify.INotify()

        self._publisher_id = None
        self._requested_value_internal = -1
        self._reset_publisher = False
        self._last_value = -1

    @inlineCallbacks
    def onJoin(self, details):
        if not _BrightnessControl.has_backlight():
            return

        self.notifier.startReading()
        self.notifier.watch(
            filepath.FilePath(BRIGHTNESS_CONFIG_FILE),
            mask=inotify.IN_CHANGED,
            callbacks=[self.publish_brightness_changed]
        )

        yield self.register(self.set_brightness, 'org.deskconn.brightness.set')
        yield self.register(self.controller.get_current_brightness_percentage, 'org.deskconn.brightness.get')

    def onLeave(self, details):
        if not _BrightnessControl.has_backlight():
            return

        self.notifier.stopReading()

    @inlineCallbacks
    def set_brightness(self, percentage, publisher_id=None):
        def actually_set_brightness(percent, pub_id):
            self._publisher_id = pub_id
            self._requested_value_internal = self.controller.percent_to_internal(percent)
            self.controller.set_brightness(percent)

        res = yield threads.deferToThread(actually_set_brightness, percentage, publisher_id)
        returnValue(res)

    def publish_brightness_changed(self, _ignored, file_path, _mask):
        with open(file_path.path) as file:
            current_value = int(file.read().strip())

            # Inotify sometimes notifies duplicate events, we only
            # want to publish a "brightness_changed" when its unique
            # since last "change".
            if current_value == self._last_value:
                return

            self.publish("org.deskconn.brightness.on_changed",
                         percentage=int((current_value / self.controller.max_brightness) * 100),
                         publisher_id=self._publisher_id)

            # Reset the "publisher" once brightness is set to the requested
            # level so that any internal change (bright changes using keyboard buttons)
            # doesn't do a publish with false "publisher".
            if current_value == self._requested_value_internal:
                self._publisher_id = None

            self._last_value = current_value

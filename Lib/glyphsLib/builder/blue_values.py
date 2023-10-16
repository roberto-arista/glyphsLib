# Copyright 2015 Google Inc. All Rights Reserved.
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

from glyphsLib.util import pairs


def to_ufo_blue_values(self, ufo, master):
    """Set postscript blue values from Glyphs alignment zones."""

    blue_values = master.blueValues
    other_blues = master.otherBlues
    if blue_values:
        ufo.info.postscriptBlueValues = blue_values
    if other_blues:
        ufo.info.postscriptOtherBlues = other_blues


def to_glyphs_blue_values(self, ufo, master):
    """Sets the GSFontMaster alignmentZones from the postscript blue values."""

    zones = []
    blue_values = pairs(ufo.info.postscriptBlueValues or [])
    other_blues = pairs(ufo.info.postscriptOtherBlues or [])
    for y1, y2 in blue_values:
        size = y2 - y1
        if y2 == 0:
            pos = 0
            size = -size
        else:
            pos = y1
        zones.append(self.glyphs_module.GSAlignmentZone(pos, size))
    for y1, y2 in other_blues:
        size = y1 - y2
        pos = y2
        zones.append(self.glyphs_module.GSAlignmentZone(pos, size))

    master.alignmentZones = sorted(zones, key=lambda zone: -zone.position)

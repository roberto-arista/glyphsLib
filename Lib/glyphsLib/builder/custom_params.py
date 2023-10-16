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


from collections import defaultdict
import re
import logging

from glyphsLib.util import bin_to_int_list, int_list_to_bin, isList

from .filters import parse_glyphs_filter
from .common import to_ufo_color
from .constants import (
    GLYPHS_PREFIX,
    GLYPH_ORDER_KEY,
    UFO2FT_COLOR_PALETTES_KEY,
    UFO2FT_FILTERS_KEY,
    UFO2FT_USE_PROD_NAMES_KEY,
    CODEPAGE_RANGES,
    REVERSE_CODEPAGE_RANGES,
    PUBLIC_PREFIX,
    UFO_FILENAME_CUSTOM_PARAM,
    UFO2FT_META_TABLE_KEY,
)
from .features import replace_feature, replace_prefixes
from glyphsLib.classes import GSCustomParameter, GSFont, GSFontMaster

"""Set Glyphs custom parameters in UFO info or lib, where appropriate.

Custom parameter data will be extracted from a Glyphs object such as GSFont,
GSFontMaster or GSInstance by wrapping it in the GlyphsObjectProxy.
This proxy normalizes and speeds up the API used to access custom parameters,
and also keeps track of which customParameters have been read from the object.

Note:
    In the special case of GSInstance -> UFO, the source object is not
    actually the GSInstance but a designspace InstanceDescriptor wrapped in
    InstanceDescriptorAsGSInstance. This is because the generation of
    instance UFOs from a Glyphs font happens in two steps:

        1. the GSFont is turned into a designspace + master UFOS
        2. the designspace + master UFOs are interpolated into instance UFOs

    We want step 2. to rely only on information from the designspace, that's why
    we use the InstanceDescriptor as a source of customParameters to put into
    the instance UFO.

In the other direction, put information from UFO info or lib into a GSFont or a
GSFontMaster. The UFO source is wrapped in a UFOProxy that records which
attributes are read/written.

In order to go in both directions, each known parameter is managed by a
ParamHandler object that can implement special rules to translate the value
between Glyphs and UFO formats. This files aims at providing at least one
handler per defined UFO info attribute, plus a bunch of handlers for known
Custom Paramerters or known UFO lib elements.

To go for example from UFO to Glyphs, each registered ParamHandler is called,
and each tries to find its parameter in the UFO's info or lib data. Accesses to
the UFO lib are recorded by the UFO proxy. After all registered ParamHandlers
have worked, we know which UFO lib fields have been "consumed" in a smart way,
and we can stupidly copy the other ones over to the Glyphs side. Same when
going from Glyphs to UFOs.
"""

CUSTOM_PARAM_PREFIX = GLYPHS_PREFIX + "customParameter."


logger = logging.getLogger(__name__)


def identity(value):
    return value


class UFOProxy:
    """Record access to the UFO's lib custom parameters"""

    def __init__(self, ufo):
        self._owner = ufo
        self._handled = set()

    def has_info_attr(self, name):
        return hasattr(self._owner.info, name)

    def get_info_value(self, name):
        return getattr(self._owner.info, name)

    def set_info_value(self, name, value):
        setattr(self._owner.info, name, value)

    def has_lib_key(self, name):
        return name in self._owner.lib

    def get_lib_value(self, name):
        if name not in self._owner.lib:
            return None
        self._handled.add(name)
        return self._owner.lib[name]

    def set_lib_value(self, name, value):
        self._owner.lib[name] = value

    def unhandled_lib_items(self):
        for key, value in self._owner.lib.items():
            if key.startswith(CUSTOM_PARAM_PREFIX) and key not in self._handled:
                yield (key, value)


class AbstractParamHandler:
    # @abstractmethod
    def to_glyphs(self):
        pass

    # @abstractmethod
    def to_ufo(self):
        pass


class ParamHandler(AbstractParamHandler):
    def __init__(
        self,
        glyphs_name,
        ufo_name=None,
        glyphs_long_name=None,
        #glyphs_multivalued=False,
        glyphs3_property=None,
        ufo_prefix=CUSTOM_PARAM_PREFIX,
        ufo_info=True,
        ufo_default=None,
        value_to_ufo=identity,
        value_to_glyphs=identity,
        write_to_ufo=True, # for supporting older lib keys
        write_to_glyphs=True,
        glyphs_owner_class=None # to distingish where things are allowed to go
    ):
        self.glyphs_name = glyphs_name
        self.glyphs_long_name = glyphs_long_name
        #self.glyphs_multivalued = glyphs_multivalued
        self.glyphs3_property = glyphs3_property
        # By default, they have the same name in both
        self.ufo_name = ufo_name or glyphs_name
        self.ufo_prefix = ufo_prefix
        self.ufo_info = ufo_info
        self.ufo_default = ufo_default
        # Value transformation functions
        self.value_to_ufo = value_to_ufo
        self.value_to_glyphs = value_to_glyphs
        self.write_to_ufo = write_to_ufo
        self.write_to_glyphs = write_to_glyphs
        self.glyphs_owner_class = glyphs_owner_class

    # By default, the parameter is read from/written to:
    #  - the Glyphs object's customParameters
    #  - the UFO's info object if it has a matching attribute, else the lib
    def to_glyphs(self, glyphs, ufo):
        if self.glyphs_owner_class and not isinstance(glyphs._owner, self.glyphs_owner_class): # some parameters should only be set either in font or on master
            return
        if not self.write_to_glyphs:
            return
        ufo_value = self._read_from_ufo(glyphs, ufo)

        if ufo_value is None:
            return
        glyphs_value = self.value_to_glyphs(ufo_value)
        self._write_to_glyphs(glyphs, glyphs_value)

    def to_ufo(self, builder, glyphs, ufo):
        if not self.write_to_ufo:
            return
        glyphs_value = self._read_from_glyphs(glyphs)
        if glyphs_value is None:
            return
        ufo_value = self.value_to_ufo(glyphs_value)
        if ufo_value is not None:
            self._write_to_ufo(glyphs, ufo, ufo_value)

    def _read_from_glyphs(self, glyphs):
        value = None
        # Try to read from the properties first.
        if self.glyphs3_property is not None:
            value = glyphs[self.glyphs3_property]
            if value is not None:
                return value
        value = glyphs[self.glyphs_name]
        if value is not None:
            return value
        if self.glyphs_long_name is not None:
            value = glyphs[self.glyphs_long_name]
        return value

    def _write_to_glyphs(self, glyphs, value):
        # We currently convert UFO to Glyphs2 files.
        # If we ever export Glyphs3 by default, we need a similar test
        # here to the one in _read_from_glyphs to determine whether a
        # value should be placed in the new properties top-level key.

        parameter = GSCustomParameter(self.glyphs_name, value)
        glyphs.append(parameter)

    def _read_from_ufo(self, glyphs, ufo):
        if self.ufo_info and ufo.has_info_attr(self.ufo_name):
            return ufo.get_info_value(self.ufo_name)
        else:
            ufo_prefix = self.ufo_prefix
            if ufo_prefix == CUSTOM_PARAM_PREFIX:
                ufo_prefix += glyphs.__class__.__name__+"."
            return ufo.get_lib_value(ufo_prefix + self.ufo_name)

    def _write_to_ufo(self, glyphs, ufo, value):
        if self.ufo_default is not None and value == self.ufo_default:
            return
        if self.ufo_info and ufo.has_info_attr(self.ufo_name):
            # most OpenType table entries go in the info object
            ufo.set_info_value(self.ufo_name, value)
        else:
            # everything else gets dumped in the lib
            ufo_prefix = self.ufo_prefix
            if ufo_prefix == CUSTOM_PARAM_PREFIX:
                ufo_prefix += glyphs._owner.__class__.__name__+"."
            ufo.set_lib_value(ufo_prefix + self.ufo_name, value)


KNOWN_PARAM_HANDLERS = []


def register(handler):
    KNOWN_PARAM_HANDLERS.append(handler)


GLYPHS_FONT_UFO_CUSTOM_PARAMS = (
    # These are stored in the official descriptor attributes.
    # "familyName",
    # "fileName",
    #("compatibleFullName", "openTypeNameCompatibleFullName"),
    # OS/2 parameters
    ("panose", "openTypeOS2Panose"),
    ("fsType", "openTypeOS2Type"),
    # OS/2 subscript parameters
    # PostScript parameters
    ("blueScale", "postscriptBlueScale"),
    ("blueShift", "postscriptBlueShift"),
    ("isFixedPitch", "postscriptIsFixedPitch"),
)

GLYPHS_MASTER_UFO_CUSTOM_PARAMS = (
    # These are stored in the official descriptor attributes.
    # "familyName",
    # "fileName",
    ("hheaAscender", "openTypeHheaAscender"),
    ("hheaDescender", "openTypeHheaDescender"),
    ("hheaLineGap", "openTypeHheaLineGap"),
    # OS/2 parameters
    ("typoAscender", "openTypeOS2TypoAscender"),
    ("typoDescender", "openTypeOS2TypoDescender"),
    ("typoLineGap", "openTypeOS2TypoLineGap"),
    ("unicodeRanges", "openTypeOS2UnicodeRanges"),
    ("strikeoutSize", "openTypeOS2StrikeoutSize"),
    ("strikeoutPosition", "openTypeOS2StrikeoutPosition"),
    # OS/2 subscript parameters
    ("subscriptXSize", "openTypeOS2SubscriptXSize"),
    ("subscriptYSize", "openTypeOS2SubscriptYSize"),
    ("subscriptXOffset", "openTypeOS2SubscriptXOffset"),
    ("subscriptYOffset", "openTypeOS2SubscriptYOffset"),
    # OS/2 superscript parameters
    ("superscriptXSize", "openTypeOS2SuperscriptXSize"),
    ("superscriptYSize", "openTypeOS2SuperscriptYSize"),
    ("superscriptXOffset", "openTypeOS2SuperscriptXOffset"),
    ("superscriptYOffset", "openTypeOS2SuperscriptYOffset"),
    # These can be recovered by reading the mapping backward.
    # ("weightClass", "openTypeOS2WeightClass"),
    # ("widthClass", "openTypeOS2WidthClass"),
    # These are processed separatedly down below.
    # ("winAscent", "openTypeOS2WinAscent"),
    # ("winDescent", "openTypeOS2WinDescent"),
    ("vheaVertAscender", "openTypeVheaVertTypoAscender"),
    ("vheaVertDescender", "openTypeVheaVertTypoDescender"),
    ("vheaVertLineGap", "openTypeVheaVertTypoLineGap"),
    ("vheaVertTypoAscender", "openTypeVheaVertTypoAscender"),
    ("vheaVertTypoDescender", "openTypeVheaVertTypoDescender"),
    ("vheaVertTypoLineGap", "openTypeVheaVertTypoLineGap"),
    # PostScript parameters
    ("underlinePosition", "postscriptUnderlinePosition"),
    ("underlineThickness", "postscriptUnderlineThickness"),
)

for glyphs_name, ufo_name in GLYPHS_FONT_UFO_CUSTOM_PARAMS:
    register(ParamHandler(glyphs_name, ufo_name, glyphs_long_name=ufo_name, glyphs_owner_class=GSFont))

for glyphs_name, ufo_name in GLYPHS_MASTER_UFO_CUSTOM_PARAMS:
    register(ParamHandler(glyphs_name, ufo_name, glyphs_long_name=ufo_name, glyphs_owner_class=GSFontMaster))

# Reference:
# https://github.com/googlefonts/glyphsLib/pull/881#issuecomment-1474226616
GLYPHS_UFO_CUSTOM_PARAMS_GLYPHS3_PROPERTIES = (
    # This is stored in the official descriptor attributes.
    # "familyNames",
    # TODO: Map these properties to custom parameters if applicable.
    # "designers",
    # "designerURL",
    # "manufacturers",
    # "manufacturerURL",
    # "copyrights",
    #("versionString", "openTypeNameVersion", "versionString"),
    #("vendorID", "openTypeOS2VendorID", "vendorID"),
    # TODO: Map this property to a custom parameter if applicable.
    # "uniqueID",
    #("license", "openTypeNameLicense", "licenses"),
    #("licenseURL", "openTypeNameLicenseURL", "licenseURL"),
    #("trademark", "trademark", "trademarks"),
    #("description", "openTypeNameDescription", "descriptions"),
    #("sampleText", "openTypeNameSampleText", "sampleTexts"),
    # TODO: Should the postscriptFullName or postscriptFullNames property be
    # used for the postscriptFullName custom parameter?
    # "postscriptFullNames",
    #("postscriptFullName", "postscriptFullName", "postscriptFullName"),
    # TODO: This is stored in the official descriptor attibutes. Should this
    # entry be removed?
    #("postscriptFontName", "postscriptFontName", "postscriptFontName"),
    # TODO: Map these properties to custom parameters if applicable.
    # "compatibleFullNames",
    # "styleNames",
    # "styleMapFamilyNames",
    # "styleMapStyleNames",
    #("preferredFamilyName", "openTypeNamePreferredFamilyName", "preferredFamilyNames"),
    #("preferredSubfamilyName", "openTypeNamePreferredSubfamilyName", "preferredSubfamilyNames"),
    # TODO: Map this property to a custom parameter if applicable.
    # "variableStyleNames",
    #("WWSFamilyName", "openTypeNameWWSFamilyName", "WWSFamilyName"),
    #("WWSSubfamilyName", "openTypeNameWWSSubfamilyName", "WWSSubfamilyName"),
    # TODO: Map this property to a custom parameter if applicable.
    # "variationsPostScriptNamePrefix",
)

for glyphs_name, ufo_name, property_name in GLYPHS_UFO_CUSTOM_PARAMS_GLYPHS3_PROPERTIES:
    register(
        ParamHandler(
            glyphs_name,
            ufo_name,
            glyphs_long_name=ufo_name,
            glyphs3_property=property_name,
        )
    )

# TODO: (jany) for all the following fields, check that they are stored in a
# meaningful Glyphs customParameter. Maybe they have short names?
GLYPHS_UFO_CUSTOM_PARAMS_NO_SHORT_NAME = (
    "openTypeHheaCaretSlopeRun",
    "openTypeVheaCaretSlopeRun",
    "openTypeHheaCaretSlopeRise",
    "openTypeVheaCaretSlopeRise",
    "openTypeHheaCaretOffset",
    "openTypeVheaCaretOffset",
    "openTypeHeadLowestRecPPEM",
    "openTypeHeadFlags",
    #"openTypeNameVersion",
    #"openTypeNameUniqueID",
    "openTypeOS2FamilyClass",
    "postscriptSlantAngle",
    #"postscriptUniqueID",
    # TODO: Should this be handled in `blue_values.py`?
    # "postscriptFamilyBlues",
    # "postscriptFamilyOtherBlues",
    "postscriptBlueFuzz",
    "postscriptForceBold",
    "postscriptDefaultWidthX",
    "postscriptNominalWidthX",
    "postscriptWeightName",
    "postscriptDefaultCharacter",
    "postscriptWindowsCharacterSet",
    "macintoshFONDFamilyID",
    "macintoshFONDName",
    #"styleMapFamilyName",
    #"styleMapStyleName",
)
for name in GLYPHS_UFO_CUSTOM_PARAMS_NO_SHORT_NAME:
    register(ParamHandler(name))


class EmptyListDefaultParamHandler(ParamHandler):
    def to_glyphs(self, glyphs, ufo):
        ufo_value = self._read_from_ufo(glyphs, ufo)
        # Ingore default value == empty list
        if ufo_value is None or ufo_value == []:
            return
        glyphs_value = self.value_to_glyphs(ufo_value)
        self._write_to_glyphs(glyphs, glyphs_value)


register(EmptyListDefaultParamHandler("postscriptFamilyBlues"))
register(EmptyListDefaultParamHandler("postscriptFamilyOtherBlues"))


# Convert code page numbers to OS/2 ulCodePageRange bits. Empty lists stay empty lists.
class OS2CodePageRangesParamHandler(AbstractParamHandler):
    glyphs_name = "codePageRanges"
    ufo_name = "openTypeOS2CodePageRanges"
    def to_glyphs(self, glyphs, ufo):
        ufo_codepage_bits = ufo.get_info_value("openTypeOS2CodePageRanges")
        if ufo_codepage_bits is None:
            return

        codepages = []
        unsupported_codepage_bits = []
        for codepage in ufo_codepage_bits:
            if codepage in REVERSE_CODEPAGE_RANGES:
                codepages.append(REVERSE_CODEPAGE_RANGES[codepage])
            else:
                unsupported_codepage_bits.append(codepage)

        glyphs[self.glyphs_name] = codepages
        if unsupported_codepage_bits:
            glyphs["codePageRangesUnsupportedBits"] = unsupported_codepage_bits


    def to_ufo(self, builder, glyphs, ufo):
        codepages = glyphs[self.glyphs_name]
        if codepages is None:
            codepages = glyphs[self.ufo_name]
            if codepages is None:
                return
        ufo_codepage_bits = [CODEPAGE_RANGES[int(v)] for v in codepages]
        #unsupported_codepage_bits = glyphs["codePageRangesUnsupportedBits"]
        #if unsupported_codepage_bits:
        #    ufo_codepage_bits.extend(unsupported_codepage_bits)

        ufo.set_info_value(self.ufo_name, sorted(ufo_codepage_bits))


register(OS2CodePageRangesParamHandler())

# enforce that winAscent/Descent are positive, according to UFO spec
for glyphs_name in ("winAscent", "winDescent"):
    ufo_name = "openTypeOS2W" + glyphs_name[1:]
    register(
        ParamHandler(
            glyphs_name,
            ufo_name,
            glyphs_long_name=ufo_name,
            value_to_ufo=abs,
            value_to_glyphs=abs,
        )
    )

# The value of these could be a float, and ufoLib/defcon expect an int.
for glyphs_name in ("weightClass", "widthClass"):
    ufo_name = "openTypeOS2W" + glyphs_name[1:]
    register(ParamHandler(glyphs_name, ufo_name, value_to_ufo=int))


# convert Glyphs' GASP Table to UFO openTypeGaspRangeRecords
def to_ufo_gasp_table(value):
    # XXX maybe the parser should cast the gasp values to int?
    value = {int(k): int(v) for k, v in value.items()}
    gasp_records = []
    # gasp range records must be sorted in ascending rangeMaxPPEM
    for max_ppem, gasp_behavior in sorted(value.items()):
        gasp_records.append(
            {
                "rangeMaxPPEM": max_ppem,
                "rangeGaspBehavior": bin_to_int_list(gasp_behavior),
            }
        )
    return gasp_records


def to_glyphs_gasp_table(value):
    return {
        str(record["rangeMaxPPEM"]): int_list_to_bin(record["rangeGaspBehavior"])
        for record in value
    }


register(
    ParamHandler(
        glyphs_name="GASP Table",
        ufo_name="openTypeGaspRangeRecords",
        value_to_ufo=to_ufo_gasp_table,
        value_to_glyphs=to_glyphs_gasp_table,
    )
)

register(
    ParamHandler(
        glyphs_name="gasp Table",
        ufo_name="openTypeGaspRangeRecords",
        value_to_ufo=to_ufo_gasp_table,
        value_to_glyphs=to_glyphs_gasp_table,
    )
)


# convert Glyphs' meta Table to UFO openTypeMeta
def to_ufo_meta_table(value):
    meta = {}
    # In:  {data = "de-Latn"; tag = dlng; }, {data = "sr-Cyrl"; tag = slng; }
    # Out: { "dlng": [ "de-Latn" ], "slng": [ "sr-Cyrl" ] }
    for entry in value:
        tag, data = entry["tag"], entry["data"]
        if tag in meta:
            logger.warning(
                f"Multiple '{tag}' tags in meta table; only the last one will be used"
            )

        if tag in ("appl", "bild"):
            meta[tag] = data
        else:
            meta.setdefault(tag, []).append(data)
    return meta


def to_glyphs_meta_table(value):
    meta = []
    for tag, data in value.items():
        if isinstance(data, list):
            for entry in data:
                meta.append({"tag": tag, "data": entry})
        else:
            meta.append({"tag": tag, "data": data})
    return meta


register(
    ParamHandler(
        glyphs_name="meta Table",
        ufo_name=UFO2FT_META_TABLE_KEY,
        ufo_info=False,
        ufo_prefix="",
        value_to_ufo=to_ufo_meta_table,
        value_to_glyphs=to_glyphs_meta_table,
    )
)


def to_ufo_color_palettes(value):
    return [[to_ufo_color(color) for color in palette] for palette in value]


def _to_glyphs_color(color):
    if color[0] == color[1] == color[2]:
        color = [color[0], color[3]]
    return ",".join(str(round(v * 255)) for v in color)


def to_glyphs_color_palettes(value):
    return [[_to_glyphs_color(color) for color in palette] for palette in value]


register(
    ParamHandler(
        glyphs_name="Color Palettes",
        ufo_name=UFO2FT_COLOR_PALETTES_KEY,
        ufo_info=False,
        ufo_prefix="",
        value_to_ufo=to_ufo_color_palettes,
        value_to_glyphs=to_glyphs_color_palettes,
    )
)


class NameRecordParamHandler(AbstractParamHandler):
    glyphs_name = "Name Table Entry"
    ufo_name = "openTypeNameRecords"
    def to_entry(self, record):
        identifiers = [
            record["nameID"],
            record["platformID"],
            record["encodingID"],
            record["languageID"],
        ]
        encoding = " ".join(map(str, identifiers))
        string = record["string"]

        return f"{encoding}; {string}"

    def parse_decimal(self, string):
        # In Python octal strings must start with a prefix. Glyphs
        # uses AFDKO decimal number specification which allows
        # octals starting with "0".
        if string.startswith("0x"):
            return int(string, 16)
        elif string.startswith("0"):
            return int(string, 8)
        else:
            return int(string, 10)

    # See the Glyphs manual for the Name Table Entry format:
    # https://glyphsapp.com/media/pages/learn/3ec528a11c-1634835554/glyphs-3-handbook.pdf
    def to_record(self, entry):
        # Split only on the first semicolon occurance. Glyphs doesn't
        # have any special escaping, so anything after the first
        # semicolon is treated as part of the name table entry.
        parts = entry.split(";", 1)

        if len(parts) != 2:
            logger.warning(f"Invalid Name Table Entry '{entry}' ignored.")
        else:
            identifiers = parts[0].split(" ")
            # Strip whitespace. This behaviour is undefined, but
            # it seems sensible to remove leading and trailing spaces.
            string = parts[1].strip()

            try:
                name_id = self.parse_decimal(identifiers[0])
                platform_id = 3  # Windows
                encoding_id = 1  # Unicode BMP
                language_id = 0x409  # English, United States

                if len(identifiers) >= 2:
                    platform_id = self.parse_decimal(identifiers[1])

                if len(identifiers) >= 3:
                    encoding_id = self.parse_decimal(identifiers[2])
                elif platform_id == 1:
                    encoding_id = 0

                if len(identifiers) >= 4:
                    language_id = self.parse_decimal(identifiers[3])
                elif platform_id == 1:
                    language_id = 0

                return {
                    "nameID": name_id,
                    "platformID": platform_id,
                    "encodingID": encoding_id,
                    "languageID": language_id,
                    "string": string,
                }
            except ValueError:
                logger.warning(f"Invalid name table identifiers '{parts[0]}'.")

    def to_glyphs(self, glyphs, ufo):
        if glyphs.is_font():
            records = ufo.get_info_value(self.ufo_name)
            if records:
                entries = [self.to_entry(record) for record in records]
                for entry in entries:
                    glyphs.append(GSCustomParameter(self.glyphs_name, entry))

    def to_ufo(self, builder, glyphs, ufo):
        for entrie in glyphs:
            if entrie.name == self.glyphs_name:
                records = ufo.get_info_value(self.ufo_name) or []
                record = self.to_record(entry.value)
                if record is not None:
                    records.append(record)
                ufo.set_info_value(self.ufo_name, records)


register(NameRecordParamHandler())


register(ParamHandler(glyphs_name="Disable Last Change", ufo_name="disablesLastChange"))

register(
    ParamHandler(
        # convert between Glyphs.app's and ufo2ft's equivalent parameter
        glyphs_name="Don't use Production Names",
        ufo_name=UFO2FT_USE_PROD_NAMES_KEY,
        ufo_prefix="",
        value_to_ufo=lambda value: not value,
        value_to_glyphs=lambda value: not value,
    )
)


class MiscParamHandler(ParamHandler):
    """Copy GSFont attributes to ufo lib"""

    def _read_from_glyphs(self, glyphs):
        return glyphs[self.glyphs_name]

    def _write_to_glyphs(self, glyphs, value):
        if hasattr(glyphs, self.glyphs_name):
           setattr(glyphs, self.glyphs_name, value)


register(MiscParamHandler(glyphs_name="disablesAutomaticAlignment", ufo_prefix="com.schriftgestaltung."))
register(MiscParamHandler(glyphs_name="disablesAutomaticAlignment", write_to_ufo=False))
register(MiscParamHandler(glyphs_name="iconName", ufo_prefix="com.schriftgestaltung.", value_to_ufo=lambda value: value if value is not None and len(value) > 0 and value != "Regular" else None))
register(MiscParamHandler(glyphs_name="iconName", write_to_ufo=False))


class DisplayStringsParamHandler(MiscParamHandler):
    def __init__(self):
        super().__init__(glyphs_name="DisplayStrings")

    def to_ufo(self, builder, glyphs, ufo):
        # We test for builder here because apply_instance_data() passes None and
        # we don't want to copy-paste or subclass UFOBuilder.
        if (
            builder is not None
            and builder.store_editor_state
            and builder.font.displayStrings
        ):
            super().to_ufo(builder, glyphs, ufo)


register(DisplayStringsParamHandler())

# deal with any Glyphs naming quirks here
register(
    MiscParamHandler(
        glyphs_name="disablesNiceNames",
        ufo_name="useNiceNames",
        value_to_ufo=lambda value: bool(not value),
        value_to_glyphs=lambda value: not bool(value),
        ufo_prefix="com.schriftgestaltung.",
    )
)
register(
    MiscParamHandler(
        glyphs_name="disablesNiceNames",
        ufo_name="useNiceNames",
        value_to_ufo=lambda value: bool(not value),
        value_to_glyphs=lambda value: not bool(value),
        write_to_ufo=False,
    )
)

for number in ("", "1", "2", "3"):
    register(MiscParamHandler("customValue" + number, ufo_info=False, ufo_default=0))
register(MiscParamHandler("weightValue", ufo_info=False, ufo_default=100))
register(MiscParamHandler("widthValue", ufo_info=False, ufo_default=100))


def append_unique(array, value):
    if value not in array:
        array.append(value)


class OS2SelectionParamHandler(AbstractParamHandler):
    glyphs_name = None
    ufo_name = "openTypeOS2Selection"
    flags = {7: "Use Typo Metrics", 8: "Has WWS Names"}

    # Note that en empty openTypeOS2Selection list should stay an empty list, as
    # opposed to a non-existant list. In the latter case, we round-trip nothing, in the
    # former, we at least write an empty list to openTypeOS2SelectionUnsupportedBits
    # which we use to re-instate an empty list in the UFO on tripping back.
    def to_glyphs(self, glyphs, ufo):
        ufo_flags = ufo.get_info_value(self.ufo_name)
        if ufo_flags is None:
            return

        unsupported_bits = []
        for flag in ufo_flags:
            if flag in self.flags:
                glyphs[self.flags[flag]] = True
            else:
                unsupported_bits.append(flag)
        glyphs["openTypeOS2SelectionUnsupportedBits"] = unsupported_bits

    def to_ufo(self, builder, glyphs, ufo):
        use_typo_metrics = glyphs[self.flags[7]]
        has_wws_name = glyphs[self.flags[8]]
        unsupported_bits = glyphs["openTypeOS2SelectionUnsupportedBits"]
        if not use_typo_metrics and not has_wws_name and unsupported_bits is None:
            return

        selection_bits = ufo.get_info_value(self.ufo_name) or []
        if use_typo_metrics:
            selection_bits.append(7)
        if has_wws_name:
            selection_bits.append(8)
        if unsupported_bits:
            selection_bits.extend(unsupported_bits)
        ufo.set_info_value(self.ufo_name, sorted(set(selection_bits)))


register(OS2SelectionParamHandler())


class GlyphOrderParamHandler(AbstractParamHandler):
    """Translate between Glyphs.app's glyphOrder parameter and UFO's
    public.glyphOrder.

    See the GlyphOrderTest class for a thorough explanation.
    """
    glyphs_name = "glyphOrder"
    ufo_name = GLYPH_ORDER_KEY

    def to_glyphs(self, glyphs, ufo):
        if glyphs.is_font():
            ufo_glyphOrder = ufo.get_lib_value(self.ufo_name)
            use_glyphOrder = ufo.get_lib_value("com.schriftgestaltung.useGlyphOrder")
            if (use_glyphOrder is None or use_glyphOrder) and ufo_glyphOrder:
                glyphs[self.glyphs_name] = ufo_glyphOrder

    def to_ufo(self, builder, glyphs, ufo):
        if glyphs.is_font():
            glyphs_glyphOrder = glyphs[self.glyphs_name]
            if glyphs_glyphOrder:
                ufo_glyphOrder = ufo.get_lib_value(self.ufo_name)
                # If the custom parameter provides partial coverage we want to
                # append the original glyph order for uncovered glyphs.
                glyphs_glyphOrder += [
                    g for g in ufo_glyphOrder if g not in glyphs_glyphOrder
                ]
                ufo.set_lib_value(self.ufo_name, glyphs_glyphOrder)


register(GlyphOrderParamHandler())


class FilterParamHandler(AbstractParamHandler):
    """Handler for (Pre)Filter custom paramters.

    This is complicated. ufo2ft grew filter modules to mimic some of Glyph's
    automatic features, but due to the impedance mismatch between the flow of
    data in Glyphs and in UFOs plus Designspaces, they need to be handled in
    two ways: once for filters that should be applied to masters and once for
    filters on instances, which should be applied only to interpolated UFOs:

       +------+
       |GSFont+-------------------+
       +----+-+                   |
            |                     |
          +-+-----------+       +-+----------+
          |GSFontMaster |       |GSInstance   |
          +-------------+       +------------+
           userData                    customParameters
             com...ufo2ft.filters        Filter & PreFilter

                ^  |                      |  ^
     roundtrips |  |                      |  |
                |  v                      |  |
            lib                           |  | roundtrips
              com...ufo2ft.filters        |  |
          +-----------+                   v  |
          |Master UFO |          lib
          +---+-------+            com.schriftgestaltung.customParameter...
              |
          +---+-----+        +----------+                    +-----------------+
          | Source  |        | Instance |    ------------>   |Interpolated UFO |
          +---+-----+        +-----+----+                    +-----------------+
              |                    |          goes 1 way        lib
      +-------+-----+              |     apply_instance_data()    com...ufo2ft.filters
      | Designspace +--------------+
      +-------------+

    The ufo2ft filters should roundtrip as-is between UFO source masters and
    GSFontMaster, because that's how we use them in the UFO workflow with 1
    master UFO = 1 final font with filters applied.

    The Glyphs filters defined on GSInstance should keep doing what they were
    doing already:

    - first be copied as-is into the designspace instance's lib, which should
      roundtrip back to Glyphs
    - then be converted to ufo2ft equivalents and put in the final interpolated
      UFOs before they are compiled into final fonts. Those should not
      roundtrip because the interpolated UFO is discarded after compilation.

    The handler below only handles the latter, one-way case. Since ufo2ft
    filters are a UFO lib key, they are automatically stored in a master's
    userData by another code path.
    """
    glyphs_name = "Filter"
    ufo_name = UFO2FT_FILTERS_KEY
    def to_glyphs(self, glyphs, ufo):
        pass

    def to_ufo(self, builder, glyphs, ufo):
        if not glyphs.is_font():
            ufo_filters = []
            for pre_filter in glyphs.get_custom_values("PreFilter"):
                ufo_filters.append(parse_glyphs_filter(pre_filter, is_pre=True))
            for filter in glyphs.get_custom_values("Filter"):
                ufo_filters.append(parse_glyphs_filter(filter, is_pre=False))

            if not ufo_filters:
                return
            if not ufo.has_lib_key(self.ufo_name):
                ufo.set_lib_value(self.ufo_name, [])
            existing = ufo.get_lib_value(self.ufo_name)
            existing.extend(ufo_filters)


register(FilterParamHandler())


class ReplacePrefixParamHandler(AbstractParamHandler):
    glyphs_name = "Replace Prefix"
    ufo_name = None
    def to_ufo(self, builder, glyphs, ufo):
        repl_map = {}
        for value in glyphs.get_custom_values(self.glyphs_name):
            prefix_name, prefix_code = re.split(r"\s*;\s*", value, 1)
            # if multiple 'Replace Prefix' custom params replace the same
            # prefix, the last wins
            repl_map[prefix_name] = prefix_code

        features_text = ufo._owner.features.text

        if not (repl_map and features_text):
            return

        glyph_names = set(ufo._owner.keys())

        ufo._owner.features.text = replace_prefixes(
            repl_map, features_text, glyph_names=glyph_names
        )

    def to_glyphs(self, glyphs, ufo):
        # do the same as ReplaceFeatureParamHandler.to_glyphs
        pass


register(ReplacePrefixParamHandler())


class ReplaceFeatureParamHandler(AbstractParamHandler):
    glyphs_name = "Replace Feature"
    ufo_name = None
    def to_ufo(self, builder, glyphs, ufo):
        for value in glyphs.get_custom_values(self.glyphs_name):
            tag, repl = re.split(r"\s*;\s*", value, 1)
            ufo._owner.features.text = replace_feature(
                tag, repl, ufo._owner.features.text or ""
            )

    def to_glyphs(self, glyphs, ufo):
        # TODO: (jany) The "Replace Feature" custom parameter can be used to
        # have one instance with different features than what is stored
        # in the GSFont. When going from several UFOs to one GSFont, we could
        # detect when UFOs have different features, put the common ones in
        # GSFont and replace the different ones with this custom parameter.
        # See the file `tests/builder/features_test.py`.
        pass


register(ReplaceFeatureParamHandler())


class ReencodeGlyphsParamHandler(AbstractParamHandler):
    """The "Reencode Glyphs" custom parameter contains a list of
    'glyphname=unicodevalue' strings: e.g., ["smiley=E100", "logo=E101"].
    It only applies to specific instance (not to master or globally) and is
    meant to assign Unicode values to glyphs with the specied name at export
    time.
    When the Unicode value in question is already assigned to another glyph,
    the latter's Unicode value is deleted.
    When the Unicode value is left out, e.g., "f_f_i=", "f_f_j=", this will
    strip "f_f_i" and "f_f_j" of their Unicode values.

    This parameter handler only handles going from Glyphs to (instance) UFOs,
    and not also in the opposite direction, as the parameter isn't stored in
    the UFO lib, but directly applied to the UFO unicode values.
    """
    glyphs_name = "Reencode Glyphs"
    ufo_name = None

    def to_ufo(self, builder, glyphs, ufo):
        # TODO Check that the wrapped glyphs object is indeed an instance, and
        # not a GSFont or GSMaster (unlikely)
        reencode_list = glyphs.get_custom_value(self.glyphs_name)
        if not reencode_list:
            return
        ufo = ufo._owner
        cmap = {glyph.unicode: glyph.name for glyph in ufo}
        for entry in reencode_list:
            name, hexcode = entry.split("=")
            if name not in ufo:
                continue
            if hexcode.strip() == "":
                ufo[name].unicode = None
            else:
                codepoint = int(hexcode, 16)
                if codepoint in cmap:
                    previous = cmap[codepoint]
                    ufo[previous].unicode = None
                ufo[name].unicode = codepoint

    def to_glyphs(self, glyphs, ufo):
        # The 'Reencode Glyphs' parameter only applies to instances, which
        # are not meant to be roundtripped. No need to handle it here.
        pass


register(ReencodeGlyphsParamHandler())


class RenameGlyphsParamHandler(AbstractParamHandler):
    """The "Rename Glyphs" custom parameter contains a list of
    'glyphname=glyphname' strings: e.g., ["a=b", "b=a"].
    It only applies to specific instance (not to master or globally).

    The glyph data is swapped, but the unicode assignments remain the
    same.
    """
    glyphs_name = "Rename Glyphs"
    ufo_name = None

    def to_ufo(self, builder, glyphs, ufo):
        rename_list = glyphs.get_custom_value(self.glyphs_name)
        if not rename_list:
            return
        ufo = ufo._owner
        for entry in rename_list:
            oldname, newname = entry.split("=")
            ufo[newname], ufo[oldname] = ufo[oldname], ufo[newname]
            ufo[newname].unicodes, ufo[oldname].unicodes = (
                ufo[oldname].unicodes,
                ufo[newname].unicodes,
            )

    def to_glyphs(self, glyphs, ufo):
        # The 'Reencode Glyphs' parameter only applies to instances, which
        # are not meant to be roundtripped. No need to handle it here.
        pass


register(RenameGlyphsParamHandler())


def to_ufo_custom_params(self, ufo, glyphs_object, class_key):
    # glyphs_module=None because we shouldn't instanciate any Glyphs classes
    glyphs_proxy = glyphs_object.customParameters
    ufo_proxy = UFOProxy(ufo)

    #glyphs_proxy.mark_handled(UFO_FILENAME_CUSTOM_PARAM)
    _handled = []
    for handler in KNOWN_PARAM_HANDLERS:
        handler.to_ufo(self, glyphs_proxy, ufo_proxy)
        _handled.append(handler.glyphs_name)
        _handled.append(handler.ufo_name)
    parameters = []
    for param in glyphs_proxy:
        if param.name in _handled:
            continue
        name = _normalize_custom_param_name(param.name)
        value = _normalize_custom_param_value(param.value)
        parameters.append({"name":name, "value":value})
    if len(parameters) > 0:
        key = GLYPHS_PREFIX + class_key + ".customParameters"
        ufo.lib[key] = parameters

    _set_default_params(ufo)


def to_glyphs_custom_params(self, ufo, glyphs_object, class_key):
    glyphs_proxy = glyphs_object.customParameters
    ufo_proxy = UFOProxy(ufo)

    # Handle known parameters
    for handler in KNOWN_PARAM_HANDLERS:
        handler.to_glyphs(glyphs_proxy, ufo_proxy)

    # Since all UFO `info` entries (from `fontinfo.plist`) have a registered
    # handler, the only place where we can find unexpected stuff is the `lib`.
    # See the file `tests/builder/fontinfo_test.py` for `fontinfo` coverage.

    key = GLYPHS_PREFIX + class_key + ".customParameters"
    if key in ufo.lib:
        for cp_dict in ufo.lib[key]:
            glyphs_object.customParameters.append(GSCustomParameter(cp_dict["name"], cp_dict["value"]))

    prefix = GLYPHS_PREFIX + class_key + ".customParameters"
    for name, value in ufo_proxy.unhandled_lib_items():
        name = _normalize_custom_param_name(name)
        if not name.startswith(prefix):
            continue
        name = name[len(prefix) :]
        parameter = GSCustomParameter(name, value)
        glyphs_proxy.append(parameter)

    _unset_default_params(glyphs_object)


def _normalize_custom_param_name(name):
    """Replace curved quotes with straight quotes in a custom parameter name.
    These should be the only keys with problematic (non-ascii) characters,
    since they can be user-generated.
    """

    replacements = (("\u2018", "'"), ("\u2019", "'"), ("\u201C", '"'), ("\u201D", '"'))
    for orig, replacement in replacements:
        name = name.replace(orig, replacement)
    return name

def _normalize_custom_param_value(value):
    """
    replace custom object with a dict representation of themselves
    """
    if isList(value):
        new_value = []
        for item in value:
            new_item = _normalize_custom_param_value(item)
            new_value.append(new_item)
        return new_value
    try:
        return value.propertyListValueFormat_(3) # TODO: this is the plain Glyphs API. pythonize this
    except:
        from objc._pythonify import OC_PythonLong, OC_PythonFloat
        if isinstance(value, OC_PythonLong):
            return int(value)
        elif isinstance(value, OC_PythonFloat):
            return float(value)
        return value

DEFAULT_PARAMETERS = (
    # ufo2ft defaults to fsType Bit 2 ("Preview & Print embedding"), while
    # Glyphs.app defaults to Bit 3 ("Editable embedding")
    ("fsType", "openTypeOS2Type", [3]),
    # Reference:
    # https://glyphsapp.com/content/1-get-started/2-manuals/
    # 1-handbook-glyphs-2-0/Glyphs-Handbook-2.3.pdf#page=200
    ("underlineThickness", "postscriptUnderlineThickness", 50),
    ("underlinePosition", "postscriptUnderlinePosition", -100),
)


def _set_default_params(ufo):
    """Set Glyphs.app's default parameters when different from ufo2ft ones."""
    for _, ufo_name, default_value in DEFAULT_PARAMETERS:
        if getattr(ufo.info, ufo_name) is None:
            if isinstance(default_value, list):
                # Prevent problem if the same default value list is put in
                # several unrelated objects.
                default_value = default_value[:]
            setattr(ufo.info, ufo_name, default_value)


def _unset_default_params(glyphs):
    """Unset Glyphs.app's parameters that have default values.
    FIXME: (jany) maybe this should be taken care of in the writer? and/or
        classes should have better default values?
    """
    for glyphs_name, _, default_value in DEFAULT_PARAMETERS:
        if (
            glyphs_name in glyphs.customParameters
            and glyphs.customParameters[glyphs_name] == default_value
        ):
            del glyphs.customParameters[glyphs_name]
        # These parameters can be referred to with the two names in Glyphs
        if (
            glyphs_name in glyphs.customParameters
            and glyphs.customParameters[glyphs_name] == default_value
        ):
            del glyphs.customParameters[glyphs_name]


class GSFontParamHandler(ParamHandler):
    def to_glyphs(self, glyphs, ufo):
        if not glyphs.is_font():
            return
        super().to_glyphs(glyphs, ufo)

    def to_ufo(self, builder, glyphs, ufo):
        if not glyphs.is_font():
            return
        super().to_ufo(builder, glyphs, ufo)


# 'Virtual Master' params are GSFont-only and multi-valued (i.e. there can be multiple
# custom parameters named 'Virtual Master'); we know we want them stored in lib.plist
# hence ufo_info=False
register(
    GSFontParamHandler(
        "Virtual Master", ufo_info=False, ufo_default=[], glyphs_multivalued=True
    )
)

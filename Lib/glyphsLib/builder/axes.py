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


import logging

from fontTools.varLib.models import piecewiseLinearMap

try:
    from GlyphsApp import GSFontMaster, GSAxis, GSInstance, GSCustomParameter
except ImportError:
    from glyphsLib.classes import GSFontMaster, GSAxis, GSInstance, GSCustomParameter

from glyphsLib.classes import WEIGHT_CODES, WIDTH_CODES, InstanceType
from glyphsLib.builder.constants import WIDTH_CLASS_TO_VALUE

logger = logging.getLogger(__name__)


def class_to_value(axis, ufo_class):
    """
    >>> class_to_value('wdth', 7)
    125
    """
    if axis == "wght":
        # 600.0 => 600, 250 => 250
        return int(ufo_class)
    elif axis == "wdth":
        return WIDTH_CLASS_TO_VALUE[int(ufo_class)]

    raise NotImplementedError


def _nospace_lookup(dict, key):
    try:
        return dict[key]
    except KeyError:
        # Even though the Glyphs UI strings are supposed to be fixed,
        # some Noto files contain variants of them that have spaces.
        key = "".join(str(key).split())
        return dict[key]


def user_loc_string_to_value(axis_tag, user_loc):
    """Go from Glyphs UI strings to user space location.
    Returns None if the string is invalid.

    >>> user_loc_string_to_value('wght', 'ExtraLight')
    200
    >>> user_loc_string_to_value('wdth', 'SemiCondensed')
    87.5
    >>> user_loc_string_to_value('wdth', 'Clearly Not From Glyphs UI')
    """
    if axis_tag == "wght":
        if isinstance(user_loc, int):
            value = user_loc
        else:
            try:
                value = _nospace_lookup(WEIGHT_CODES, user_loc)
            except KeyError:
                return None
        return class_to_value("wght", value)
    elif axis_tag == "wdth":
        if isinstance(user_loc, int):
            value = user_loc
        else:
            try:
                value = _nospace_lookup(WIDTH_CODES, user_loc)
            except KeyError:
                return None
        return class_to_value("wdth", value)

    # Currently this function should only be called with a width or weight
    raise NotImplementedError


def user_loc_value_to_class(axis_tag, user_loc):
    """Return the OS/2 weight or width class that is closest to the provided
    user location. For weight the user location is between 0 and 1000 and for
    width it is a percentage.

    >>> user_loc_value_to_class('wght', 310)
    310
    >>> user_loc_value_to_class('wdth', 62)
    2
    """
    if axis_tag == "wght":
        return int(user_loc)
    elif axis_tag == "wdth":
        return min(
            sorted(WIDTH_CLASS_TO_VALUE.items()),
            key=lambda item: abs(item[1] - user_loc),
        )[0]

    raise NotImplementedError


def user_loc_value_to_instance_string(axis_tag, user_loc):
    """Return the Glyphs UI string (from the instance dropdown) that is
    closest to the provided user location.

    >>> user_loc_value_to_instance_string('wght', 430)
    'Normal'
    >>> user_loc_value_to_instance_string('wdth', 150)
    'Extra Expanded'
    """
    codes = {}
    if axis_tag == "wght":
        codes = WEIGHT_CODES
    elif axis_tag == "wdth":
        codes = WIDTH_CODES
    else:
        raise NotImplementedError
    class_ = user_loc_value_to_class(axis_tag, user_loc)
    return min(
        sorted((code, class_) for code, class_ in codes.items() if code is not None),
        key=lambda item: abs(item[1] - class_),
    )[0]


def update_mapping_from_instances(mapping, instances, axis, minimize_glyphs_diffs, cp_only=False):
    # Collect the axis mappings from instances and update the mapping dict.
    for instance in instances:
        if instance.type == InstanceType.VARIABLE:
            continue
        if instance.exports or minimize_glyphs_diffs:
            designLoc = instance.internalAxesValues[axis.axisId]
            userLoc = instance.externalAxesValues[axis.axisId]
            if userLoc is None:
                continue
            if userLoc in mapping and mapping[userLoc] != designLoc:
                logger.warning(
                    f"Axis {axis.axisTag}: Instance '{instance.name}' redefines "
                    f"the mapping for user location {userLoc} "
                    f"from {mapping[userLoc]} to {designLoc}"
                )
            mapping[userLoc] = designLoc


def is_identity(mapping):
    """Return whether the mapping is an identity mapping."""
    return all(userLoc == designLoc for userLoc, designLoc in mapping.items())


def to_designspace_axes(self):
    if not self.font.masters:
        return
    regular_master = get_regular_master(self.font)
    assert isinstance(regular_master, GSFontMaster)

    custom_mapping = self.font.customParameters["Axis Mappings"]
    virtual_masters = [
        {v["Axis"]: v["Location"] for v in cp.value}
        for cp in self.font.customParameters
        if cp.name == "Virtual Master"
    ]

    axes = self.font.axes
    for axis in axes:
        axisDescriptor = self.designspace.newAxisDescriptor()
        axisDescriptor.tag = axis.axisTag
        axisDescriptor.name = axis.name
        axisDescriptor.axisId = axis.axisId
        # TODO add support for localised axis.labelNames when Glyphs.app does

        # See https://github.com/googlefonts/glyphsLib/issues/568
        if custom_mapping:
            if axis.axisTag in custom_mapping:
                mapping = {float(k): v for k, v in custom_mapping[axis.axisTag].items()}
                regularDesignLoc = regular_master.internalAxesValues[axis.axisId]
                reverse_mapping = {dl: ul for ul, dl in sorted(mapping.items())}
                regularUserLoc = piecewiseLinearMap(regularDesignLoc, reverse_mapping)
            else:
                logger.debug(
                    f"Skipping {axis.axisTag} since it hasn't been defined "
                    "in the Axis Mapping."
                )
                continue
        # See https://github.com/googlefonts/glyphsLib/issues/280
        else:
            # If all masters have an "Axis Location" custom parameter, only the values
            # from this parameter will be used to build the mapping of the masters and
            # instances.
            mapping = {}
            for master in self.font.masters:
                designLoc = master.internalAxesValues[axis.axisId]
                userLoc = master.externalAxesValues[axis.axisId]
                if designLoc is None:
                    designLoc = 0  # TODO: (georg) this is mostly happening in tests, so better improve the test setup?
                if userLoc is None:
                    userLoc = designLoc
                if userLoc in mapping and mapping[userLoc] != designLoc:
                    logger.warning(
                        f"Axis {axis.axisTag}: Master '{master.name}' redefines "
                        f"the mapping for user location {userLoc} "
                        f"from {mapping[userLoc]} to {designLoc}"
                    )
                mapping[userLoc] = designLoc

            update_mapping_from_instances(
                mapping,
                self.font.instances,
                axis,
                minimize_glyphs_diffs=self.minimize_glyphs_diffs,
                # Glyphs doesn't deduce instance mappings if font uses axis locations.
                # Use only the custom parameter if present.
                cp_only=True,
            )

            regularDesignLoc = regular_master.internalAxesValues[axis.axisId]
            if regularDesignLoc is None:
                regularDesignLoc = 0  # TODO: (georg) this is mostly happening in tests, so better improve the test setup?
            regularUserLoc = regular_master.externalAxesValues[axis.axisId]

            if regularUserLoc is None:
                regularUserLoc = regularDesignLoc

        is_identity_map = is_identity(mapping)

        # Virtual Masters can't have an Axis Location parameter; their coordinates
        # can either be mapped via Axis Mappings, or implicitly by neighbouring
        # non-virtual masters' Axis Location params at least for existing axes; for
        # newly defined axes the virtual master coordinates are assumed to be un-mapped
        # (user==design).
        # Only if the {user:design} mapping so far is an identity map (because it
        # has not been 'bent' by one of the above mechanisms), the virtual masters
        # contribute to extend the current axis' min/max range.
        # https://github.com/googlefonts/glyphsLib/issues/859
        if is_identity_map:
            for vm in virtual_masters:
                for axis_name, axis_coord in vm.items():
                    if axis_name != axis.name:
                        continue
                    mapping[axis_coord] = axis_coord

        minimum = min(mapping)
        maximum = max(mapping)
        default = min(maximum, max(minimum, regularUserLoc))  # clamp

        if not is_identity_map:
            axisDescriptor.map = sorted(mapping.items())
        axisDescriptor.minimum = minimum
        axisDescriptor.maximum = maximum
        axisDescriptor.default = default
        self.designspace.addAxis(axisDescriptor)

    # If there are no interesting axes, but only a single master at default location
    # along all 3 predefined axes, all with identity user:design mapping, we end up
    # with an empty list of axes, which is invalid. Thus as last resort we emit a
    # do-nothing Weight axis (the default axis when no "Axes" custom parameter is
    # defined) where default==min==max==400.
    # https://github.com/googlefonts/fontmake/issues/644
    if not self.designspace.axes:
        self.designspace.addAxisDescriptor(
            name="Weight",
            tag="wght",
            minimum=0,
            default=0,
            maximum=0,
            # axisId="wght",
        )


def to_glyphs_axes(self):
    axes_parameter = []
    for axis_def in self.designspace.axes:
        if axis_def.tag == "wght":
            axes_parameter.append(
                GSAxis(name=axis_def.name or "Weight", tag="wght")
            )
        elif axis_def.tag == "wdth":
            axes_parameter.append(GSAxis(name=axis_def.name or "Width", tag="wdth"))
        else:
            axes_parameter.append(GSAxis(name=axis_def.name, tag=axis_def.tag))
    if axes_parameter:
        self._font.axes = axes_parameter

    if any(_has_meaningful_map(a, self.designspace) for a in self.designspace.axes):
        mapping = {
            axis_def.tag: {str(k): v for k, v in axis_def.map} for axis_def in self.designspace.axes
        }
        self._font.customParameters["Axis Mappings"] = mapping


def check_axis_ranges(self):
    for axis in self.font.axes:
        axis_def = self.designspace.getAxisByTag(axis.axisTag)
        assert axis_def
        minimum = 10000
        maximum = -10000
        for master in self.font.masters:
            designLoc = master.internalAxesValues[axis.axisId]
            userLoc = master.externalAxesValues[axis.axisId]
            minimum = min(userLoc or designLoc or 0, minimum)
            maximum = max(userLoc or designLoc or 0, maximum)
        for customParameter in self.font.customParameters:
            if customParameter.name == "Virtual Master":
                for location in customParameter.value:
                    if location["Axis"] == axis.name:
                        loc = location["Location"]
                        minimum = min(loc, minimum)
                        maximum = max(loc, maximum)

        if axis_def.minimum < minimum:
            self.font.customParameters.append(GSCustomParameter("Virtual Master", [{"Axis": axis.name, "Location": axis_def.minimum}]))

        if axis_def.maximum > maximum:
            self.font.customParameters.append(GSCustomParameter("Virtual Master", [{"Axis": axis.name, "Location": axis_def.maximum}]))


class AxisDefinition:
    """Centralize the code that deals with axis locations, user location versus
    design location, associated OS/2 table codes, etc.
    """

    def __init__(
        self,
        tag,
        name,
        design_loc_key,
        default_design_loc=0.0,
        user_loc_key=None,
        user_loc_param=None,
        default_user_loc=0.0,
    ):
        self.tag = tag
        self.name = name
        self.design_loc_key = design_loc_key
        self.default_design_loc = default_design_loc
        self.user_loc_key = user_loc_key
        self.user_loc_param = user_loc_param
        self.default_user_loc = default_user_loc

    def get_design_loc(self, glyphs_master_or_instance):
        """Get the design location (aka interpolation value) of a Glyphs
        master or instance along this axis. For example for the weight
        axis it could be the thickness of a stem, for the width a percentage
        of extension with respect to the normal width.
        """
        return glyphs_master_or_instance.axes[self.design_loc_key]

    def set_design_loc(self, master_or_instance, value):
        """Set the design location of a Glyphs master or instance."""
        master_or_instance.axes[self.design_loc_key] = value

    def set_user_loc(self, master_or_instance, value):
        """Set the user location of a Glyphs master or instance."""
        if isinstance(master_or_instance, GSInstance):
            # The following code is only valid for instances.
            # Masters also the keys `weight` and `width` but they should not be
            # used, they are deprecated and should only be used to store
            # (parts of) the master's name, but not its location.

            # Try to set the key if possible, i.e. if there is a key, and
            # if there exists a code that can represent the given value, e.g.
            # for "weight": 600 can be represented by SemiBold so we use that,
            # but for 550 there is no code so we will have to set the custom
            # parameter as well.
            if self.user_loc_key is not None and hasattr(
                master_or_instance, self.user_loc_key
            ):
                code = user_loc_value_to_instance_string(self.tag, value)
                value_for_code = user_loc_string_to_value(self.tag, code)
                setattr(master_or_instance, self.user_loc_key, code)
                if self.user_loc_param is not None and value != value_for_code:
                    try:
                        class_ = user_loc_value_to_class(self.tag, value)
                        master_or_instance.customParameters[
                            self.user_loc_param
                        ] = class_
                    except NotImplementedError:
                        # user_loc_value_to_class only works for weight & width
                        pass
            return

        # Directly set the custom parameter (old way) and also the Axis Location
        # (new way).
        if self.user_loc_param is not None:
            try:
                class_ = user_loc_value_to_class(self.tag, value)
                master_or_instance.customParameters[self.user_loc_param] = class_
            except NotImplementedError:
                pass

        loc_param = master_or_instance.customParameters["Axis Location"]
        if loc_param is None:
            loc_param = []
            master_or_instance.customParameters["Axis Location"] = loc_param
        location = None
        for loc in loc_param:
            if loc.get("Axis") == self.name:
                location = loc
        if location is None:
            loc_param.append({"Axis": self.name, "Location": value})
        else:
            location["Location"] = value

    def set_user_loc_code(self, instance, code):
        assert isinstance(instance, GSInstance)
        # The previous method `set_user_loc` will not roundtrip every
        # time, for example for value = 600, both "DemiBold" and "SemiBold"
        # would work, so we provide this other method to set a specific code.
        if self.user_loc_key is not None:
            setattr(instance, self.user_loc_key, code)

    def set_ufo_user_loc(self, ufo, value):
        if self.tag not in ("wght", "wdth"):
            raise NotImplementedError
        class_ = user_loc_value_to_class(self.tag, value)
        ufo_key = (
            "openTypeOS2WeightClass" if self.tag == "wght" else "openTypeOS2WidthClass"
        )
        setattr(ufo.info, ufo_key, class_)


def _has_meaningful_map(axis, designspace):
    if not axis.map:
        return False
    for k, v in axis.map:
        if k != v:
            return True
    # We have an identity map. We could elide it, but...
    # sometimes we use an identity map to force a particular
    # range even though the sources don't fill that range.
    min_axis = None
    max_axis = None
    for source in designspace.sources:
        loc = source.location.get(axis.name)
        if loc is None:
            continue
        if min_axis is None:
            min_axis = loc
        else:
            min_axis = min(loc, min_axis)
        if max_axis is None:
            max_axis = loc
        else:
            max_axis = max(loc, max_axis)
    if (min_axis and min_axis != axis.map[0][0]) or (max_axis and max_axis != axis.map[-1][0]):
        return True
    return False


def get_regular_master(font):
    """Find the "regular" master among the GSFontMasters.

    Tries to find the master with the passed 'regularName'.
    If there is no such master or if regularName is None,
    tries to find a base style shared between all masters
    (defaulting to "Regular"), and then tries to find a master
    with that style name. If there is no master with that name,
    returns the first master in the list.
    """
    if not font.masters:
        return None
    # The current glyphs source specification supports the custom
    # parameter name "Variable Font Origin".  This may have been
    # named "Variation Font Origin" in the past.
    # We support the current name with a fallback to the previous name
    # if not found in the GSFont.customParameters dict
    if "Variable Font Origin" in font.customParameters:
        regular_id = font.customParameters["Variable Font Origin"]
        if regular_id:
            for master in font.masters:
                if master.id == regular_id:
                    return master
    elif "Variation Font Origin" in font.customParameters:
        regular_name = font.customParameters["Variation Font Origin"]
        if regular_name:
            for master in font.masters:
                if master.name == regular_name:
                    return master
    base_style = find_base_style(font.masters)
    if not base_style:
        base_style = "Regular"
    for master in font.masters:
        if master.name == base_style:
            return master
    # Second try: maybe the base style has regular in it as well
    for master in font.masters:
        name_without_regular = " ".join(
            n for n in master.name.split(" ") if n != "Regular"
        )
        if name_without_regular == base_style:
            return master
    return font.masters[0]


def find_base_style(masters):
    """Find a base style shared between all masters.
    Return empty string if none is found.
    """
    if not masters:
        return ""
    base_style = (masters[0].name or "").split()
    for master in masters:
        style = master.name.split()
        base_style = [s for s in style if s in base_style]
    base_style = " ".join(base_style)
    return base_style

"""Typed device errors so collectors can react precisely."""


class DeviceError(Exception):
    """Base class for all device communication failures."""


class DeviceConnectionError(DeviceError):
    """Unreachable / refused / network-level failure."""


class DeviceTimeoutError(DeviceConnectionError):
    """The device or network did not answer in time."""


class DeviceAuthError(DeviceError):
    """Comm key rejected / device reported Unauthenticated."""


class DeviceBusyError(DeviceError):
    """Device is busy (another client / local operation in progress)."""


class DeviceCommandError(DeviceError):
    """A command was acknowledged with an error by the device."""


class UnsupportedCommandError(DeviceError):
    """Firmware does not support this command — never retried as transient."""

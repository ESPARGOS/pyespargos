import PyQt6.QtCore
from PyQt6.QtMultimedia import QMediaDevices, QCameraDevice, QCameraFormat, QCamera

class VideoCamera(QCamera):
	"QCamera which exposes relevant properties for QML."
	availableFormatsChanged = PyQt6.QtCore.pyqtSignal()

	def __init__(self, default_device: str | None = None, default_format: str | None = None):
		if default_device is not None:
			videoDevice = self._find_device(default_device)
		else:
			videoDevice = QMediaDevices.defaultVideoInput()

		super().__init__(videoDevice)

		if default_format is not None:
			fmt = self._find_format(default_format)
			self.setCameraFormat(fmt)
		else:
			availableFormats = videoDevice.videoFormats()
			fmt = availableFormats[-1]
			self.setCameraFormat(fmt)

	def setDevice(self, device_str: str):
		device = self._find_device(device_str)
		self.setCameraDevice(device)

		# Notify that available formats may have changed
		self.availableFormatsChanged.emit()

	def setFormat(self, format_str: str):
		fmt = self._find_format(format_str)
		self.setCameraFormat(fmt)

	def getDevice(self) -> str:
		return self._device_to_string(self.cameraDevice())
	
	def getFormat(self) -> str:
		return self._format_to_string(self.cameraFormat())

	def _device_to_string(self, device: QCameraDevice) -> str:
		return bytes(device.id()).decode("utf-8") + ": " + device.description()
	
	def _format_to_string(self, fmt: QCameraFormat) -> str:
		return f"{fmt.resolution().width()}x{fmt.resolution().height()} @ {fmt.maxFrameRate():.2f} FPS"

	def _find_device(self, device_str: str) -> QCameraDevice:
		"""
		Find a QCameraDevice by its string representation.
		Returns the first matching QCameraDevice.
		A match is found if device_str is *contained* (no exact match required) in the string "<id>: <description>".
		Raises ValueError if no matching device is found.
		"""
		devices = QMediaDevices.videoInputs()
		for device in devices:
			if device_str in self._device_to_string(device):
				return device
		raise ValueError(f"No camera device matching '{device_str}' found")

	def _find_format(self, format_str: str) -> QCameraFormat:
		"""
		Find a QCameraFormat by its string representation.
		Returns the first matching QCameraFormat.
		A match is found if format_str is *contained* (no exact match required) in the string "<resolution> @ <framerate>".
		Raises ValueError if no matching format is found.
		"""
		formats = self.cameraDevice().videoFormats()
		for fmt in formats:
			if format_str in self._format_to_string(fmt):
				return fmt
		raise ValueError(f"No camera format matching '{format_str}' found")

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def availableDevices(self) -> list:
		devices = QMediaDevices.videoInputs()
		return [self._device_to_string(device) for device in devices]

	@PyQt6.QtCore.pyqtProperty(list, constant=False)
	def availableFormats(self) -> list[str]:
		formats = self.cameraDevice().videoFormats()
		return [self._format_to_string(fmt) for fmt in formats]
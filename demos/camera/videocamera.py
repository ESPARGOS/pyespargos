import PyQt6.QtCore
from PyQt6.QtMultimedia import QMediaDevices, QCamera

class VideoCamera(QCamera):
	"QCamera which exposes relevant properties for QML."
	availableFormatsChanged = PyQt6.QtCore.pyqtSignal()

	def __init__(self, cameraId = None):
		videoDevices = QMediaDevices.videoInputs()
		if cameraId is not None and cameraId < len(videoDevices):
			videoDevice = videoDevices[cameraId]
		else:
			videoDevice = QMediaDevices.defaultVideoInput()

		super().__init__(videoDevice)

		if not videoDevice.isNull():
			availableFormats = videoDevice.videoFormats()
			fmt = availableFormats[-1]
			self.setCameraFormat(fmt)

	def setDevice(self, cameraId: int):
		"Set the camera device by its index in QMediaDevices.videoInputs()."
		videoDevices = QMediaDevices.videoInputs()
		if cameraId < 0 or cameraId >= len(videoDevices):
			raise ValueError(f"Invalid cameraId {cameraId}, must be between 0 and {len(videoDevices)-1}")

		videoDevice = videoDevices[cameraId]
		self.setCameraDevice(videoDevice)

		# Notify that available formats may have changed
		self.availableFormatsChanged.emit()

	def setFormat(self, formatIndex: int):
		"Set the camera format by its index in the list of available formats."
		formats = self.cameraDevice().videoFormats()
		if formatIndex < 0 or formatIndex >= len(formats):
			raise ValueError(f"Invalid formatIndex {formatIndex}, must be between 0 and {len(formats)-1}")

		fmt = formats[formatIndex]
		self.setCameraFormat(fmt)

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def availableDevices(self) -> list:
		devices = QMediaDevices.videoInputs()
		return [device.description() for device in devices]

	@PyQt6.QtCore.pyqtProperty(list, constant=False)
	def availableFormats(self) -> list[str]:
		formats = self.cameraDevice().videoFormats()
		return [f"{fmt.resolution().width()}x{fmt.resolution().height()} @ {fmt.maxFrameRate():.2f} FPS" for fmt in formats]
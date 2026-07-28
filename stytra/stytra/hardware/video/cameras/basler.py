from stytra.hardware.video.cameras.interface import Camera

try:
    from pypylon import pylon
    import pypylon.genicam as geni
except ImportError:
    pass


class BaslerCamera(Camera):
    """Class for simple control of a camera such as a webcam using opencv.
    Tested only on a simple USB Logitech 720p webcam. Exposure and framerate
    seem to work.
    Different cameras might have different problems because of the
    camera-agnostic opencv control modules. Moreover, it might not work on a
    macOS because of system-specific problems in the multiprocessing Queues().

    """

    def __init__(self, cam_idx=0, **kwargs):
        super().__init__(**kwargs)
        self.camera = pylon.InstantCamera(
            pylon.TlFactory.GetInstance().CreateFirstDevice()
        )


    def open_camera(self):
        """ """
        self.camera.Open()
        self.camera.PixelFormat.Value = "Mono8"
        self.camera.Gain.Value = 10
        self.camera.ExposureTime.Value = 4000 #cam.ExposureTime.Min
        self.camera.ChunkModeActive.Value = True
        self.camera.ChunkSelector.Value = "LineStatusAll"
        self.camera.ChunkEnable.Value = True


        self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        return ["I:Basler camera opened"]

    def set(self, param, val):
        """

        Parameters
        ----------
        param :

        val :


        Returns
        -------

        """
        # pass
        # # try:
        if param == "exposure":
            self.camera.ExposureTime = val * 1000
            return ""
        # elif param == "framerate":
        #     self.camera.FrameRate = 100
        elif param == "gain":
            self.camera.Gain = val
        else:
            return "W: " + param + " not implemented"

    def detector(self, y_vals):    
        logic_levels = ((y_vals & (1<<2)) != 0)*1
        return logic_levels

    def read(self):
        """ """
        grabResult = self.camera.RetrieveResult(
            5000, pylon.TimeoutHandling_ThrowException
        )

        if grabResult.GrabSucceeded():
            img = grabResult.Array
            time=grabResult.TimeStamp
            logic=self.detector(grabResult.ChunkLineStatusAll.Value)

            # print("Gray value of first pixel: ", img[0, 0])
            grabResult.Release()
            return img, time, logic

        else:
            return None

    def release(self):
        """ """
        pass
        # self.camera.stopGrabbing()


if __name__ == "__main__":
    camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    i = camera.GetNodeMap()

    camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
    camera.FrameRate = 10

    grabResult = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

    if grabResult.GrabSucceeded():
        # Access the image data.
        print("SizeX: ", grabResult.Width)
        print("SizeY: ", grabResult.Height)
        img = grabResult.Array
        print("Gray value of first pixel: ", img[0, 0])

    grabResult.Release()

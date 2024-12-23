# DO NOT MOVE THIS
# FATAL WEIRD CRASH IF THIS ISN'T IMPORTED FIRST DON'T ASK
from marimapper.sfm_process import SFM

from tqdm import tqdm
from pathlib import Path
from marimapper.detector_process import DetectorProcess
from multiprocessing import get_logger, Queue
from marimapper.file_tools import get_all_2d_led_maps
from marimapper.utils import get_user_confirmation
from marimapper.visualize_process import VisualiseProcess
from marimapper.led import last_view
from marimapper.file_writer_process import FileWriterProcess
import time

logger = get_logger()


class Scanner:

    def __init__(
        self,
        output_dir: Path,
        device: str,
        exposure: int,
        threshold: int,
        backend: str,
        server: str,
        led_start: int,
        led_end: int,
        infill: bool,
    ):
        logger.debug("initialising scanner")
        self.output_dir = output_dir
        self.infill = infill
        self.detector = DetectorProcess(
            device,
            exposure,
            threshold,
            backend,
            server,
        )

        self.sfm = SFM()

        self.file_writer = FileWriterProcess(self.output_dir)
        leds = get_all_2d_led_maps(self.output_dir)

        for led in leds:
            self.sfm.add_detection(led)

        self.current_view = last_view(leds) + 1

        self.renderer3d = VisualiseProcess()

        self.detector_update_queue = Queue()

        self.detector.add_output_queue(self.sfm.get_input_queue())
        self.detector.add_output_queue(self.detector_update_queue)
        self.detector.add_output_queue(self.file_writer.get_2d_input_queue())

        self.sfm.add_output_queue(self.renderer3d.get_input_queue())
        self.sfm.add_output_queue(self.file_writer.get_3d_input_queue())
        self.sfm.start()
        self.renderer3d.start()
        self.detector.start()
        self.file_writer.start()

        self.led_id_range = range(
            led_start, min(led_end, self.detector.get_led_count())
        )

        logger.debug("scanner initialised")

    def close(self):
        logger.debug("scanner closing")

        self.detector.stop()
        self.sfm.stop()
        self.renderer3d.stop()
        self.file_writer.stop()

        self.sfm.join()
        self.renderer3d.join()
        self.detector.join()
        self.file_writer.join()
        logger.debug("scanner closed")

    def mainloop(self):
        while True:
            # Wait for other processes to run so that missing leds can be detected accurately
            time.sleep(1)
            if not self.detector.get_input_queue().empty():
                continue
            if not self.sfm.get_input_queue().empty():
                continue
            if not self.file_writer.get_3d_input_queue().empty():
                continue
            if self.sfm.is_busy():
                continue

            infill_led_list = []
            if self.infill:
                # use the backed to highlight the leds, in the range, which have not been detected
                leds = self.file_writer.get_leds()
                for led_id in self.led_id_range:
                    # has the led been detected? i.e. is it in the list of 3d leds
                    if not any(led.led_id == led_id for led in leds):
                        # if NOT then show it to aid user in targeting camera vies of the missing leds
                        self.detector.show(led_id)
                        # remember that this led has been shown
                        infill_led_list.append(led_id)
                # log the number of missing leds
                logger.info(f"Missing {len(infill_led_list)} LEDs")

            start_scan = get_user_confirmation("Start scan? [y/n]: ")

            if self.infill:
                # Ensure all LEDs start in the off state
                for led_id in infill_led_list:
                    self.detector.hide(led_id)

            if not start_scan:
                print("Exiting Marimapper")
                return

            if len(self.led_id_range) == 0:
                print(
                    "LED range is zero, have you chosen a backend with 'marimapper --backend'?"
                )
                continue

            print("Starting scan")
            for led_id in self.led_id_range:
                self.detector.detect(led_id, self.current_view)

            for _ in tqdm(
                self.led_id_range,
                unit="LEDs",
                desc="Capturing sequence",
                total=self.led_id_range.stop,
                smoothing=0,
            ):
                self.detector_update_queue.get()

            self.current_view += 1

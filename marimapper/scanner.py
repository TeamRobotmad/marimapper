import os
import time
from tqdm import tqdm
from pathlib import Path

from marimapper.reconstructor import Reconstructor
from marimapper import utils
from marimapper import logging
from marimapper.utils import get_user_confirmation
from marimapper.led_map_2d import LEDMap2D
from marimapper.sfm import SFM
from marimapper.visualize_model import Renderer3D
from multiprocessing import Queue
from marimapper.led_map_2d import get_all_2d_led_maps


class Scanner:

    def __init__(self, cli_args):
        self.output_dir = cli_args.dir
        self.led_backend = utils.get_backend(cli_args.backend, cli_args.server)
        if self.led_backend is not None:
            self.led_id_range = range(
                cli_args.start, min(cli_args.end, self.led_backend.get_led_count())
            )
        os.makedirs(self.output_dir, exist_ok=True)
        self.led_map_2d_queue = Queue()
        self.led_map_3d_queue = Queue()

        self.reconstructor = Reconstructor(
            cli_args.device,
            cli_args.exposure,
            cli_args.threshold,
            self.led_backend,
            width=cli_args.width,
            height=cli_args.height,
        )

        self.renderer3d = Renderer3D(led_map_3d_queue=self.led_map_3d_queue)
        self.sfm = SFM(
            Path(self.output_dir),
            rescale=True,
            interpolate=True,
            event_on_update=self.renderer3d.reload_event,
            led_map_2d_queue=self.led_map_2d_queue,
            led_map_3d_queue=self.led_map_3d_queue,
        )

        self.led_maps_2d = get_all_2d_led_maps(Path(self.output_dir))

        self.sfm.add_led_maps_2d(self.led_maps_2d)

        self.sfm.start()
        self.renderer3d.start()

    def close(self):
        logging.debug("marimapper closing")
        self.sfm.shutdown()
        self.renderer3d.shutdown()
        self.sfm.join()
        self.renderer3d.join()
        self.sfm.terminate()
        self.renderer3d.terminate()
        self.reconstructor.close()
        logging.debug("marimapper closed")

    def mainloop(self):

        while True:

            self.reconstructor.light()
            self.reconstructor.open_live_feed()

            start_scan = get_user_confirmation("Start scan? [y/n]: ")

            self.reconstructor.close_live_feed()

            if not start_scan:
                return

            if self.led_backend is None:
                logging.warn(
                    "Cannot start backend as no backend has been defined. Re-run marimapper with --backend <backend name>"
                )
                return

            self.reconstructor.dark()

            result = self.reconstructor.find_led(debug=True)
            if result is not None:
                logging.error(
                    f"All LEDs should be off, but the detector found one at {result.pos()}"
                )
                continue

            # The filename is made out of the date, then the resolution of the camera
            string_time = time.strftime("%Y%m%d-%H%M%S")

            filepath = os.path.join(self.output_dir, f"led_map_2d_{string_time}.csv")

            led_map_2d = LEDMap2D()

            total_leds_found = 0

            visible_leds = []

            last_camera_motion_check_time = time.time()
            camera_motion_interval_sec = 5

            capture_success = True

            for led_id in tqdm(
                self.led_id_range,
                unit="LEDs",
                desc=f"Capturing sequence to {filepath}",
                total=self.led_id_range.stop,
                smoothing=0,
            ):

                result = self.reconstructor.enable_and_find_led(led_id, debug=True)

                if result:
                    visible_leds.append(led_id)
                    led_map_2d.add_detection(led_id, result)
                    total_leds_found += 1

                is_last = led_id == self.led_id_range.stop - 1
                camera_motion_check_overdue = (
                    time.time() - last_camera_motion_check_time
                ) > camera_motion_interval_sec

                if camera_motion_check_overdue or is_last:
                    camera_motion = self.reconstructor.get_camera_motion(
                        visible_leds, led_map_2d
                    )
                    last_camera_motion_check_time = time.time()

                    if camera_motion > 1.0:
                        logging.warn(f"\nCamera moved by {int(camera_motion)}%")
                        if not get_user_confirmation("Continue? [y/n]: "):
                            capture_success = False
                            break

            if capture_success:
                led_map_2d.write_to_file(filepath)
                logging.info(f"{total_leds_found}/{self.led_id_range.stop} leds found")

                self.led_maps_2d.append(led_map_2d)
                self.sfm.add_led_maps_2d(self.led_maps_2d)
                self.sfm.reload()
            else:
                logging.error("Capture failed")

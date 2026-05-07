from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable, Dict, Optional


class SafeTwoButtonMonitor:
    """
    Legacy in-app two-button monitor for the XC9022 / GoodTFT 2.8" hat.

    The current appliance build uses the external four-button sidecar:
      tools/button_sidecar.sh

    This module is kept for compatibility with earlier builds where only two
    buttons were considered safe from inside the main Python process.
    """

    FORBIDDEN = {22, 27, 24, 25}

    def __init__(
        self,
        config: dict,
        state,
        on_reset: Callable[[], None],
        on_restart: Callable[[], None],
        on_next_page: Callable[[], None],
        on_toggle_freeze: Callable[[], None],
    ) -> None:
        self.config = config
        self.state = state
        self.on_reset = on_reset
        self.on_restart = on_restart
        self.on_next_page = on_next_page
        self.on_toggle_freeze = on_toggle_freeze

        self.enabled = bool(config.get("buttons_enabled", False))
        self.poll_sec = float(config.get("button_poll_sec", 0.06))
        self.long_press_sec = float(config.get("button_long_press_sec", 1.6))
        self.reset_gpio = int(config.get("button_reset_gpio", 18))
        self.page_gpio = int(config.get("button_page_gpio", 23))
        self.use_pullup = bool(config.get("button_use_pinctrl_pullup", True))

        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

        self._pressed_at: Dict[int, float] = {}
        self._last_val: Dict[int, str] = {}

    def start(self) -> None:
        if not self.enabled:
            return

        if not self.have_pinctrl():
            self.state.append_status("Buttons disabled: /usr/bin/pinctrl not found")
            return

        used = {self.reset_gpio, self.page_gpio}
        bad = used.intersection(self.FORBIDDEN)
        if bad:
            self.state.append_status(f"Buttons disabled: forbidden GPIO(s): {sorted(bad)}")
            return

        # Only touch the two confirmed pins. Nothing else.
        for gpio in sorted(used):
            if self.use_pullup:
                self.set_input_pullup(gpio)
            self._last_val[gpio] = self.get_level(gpio)

        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.state.append_status(
            f"Safe buttons active: GPIO{self.reset_gpio}=reset/restart, GPIO{self.page_gpio}=page/freeze"
        )

    def have_pinctrl(self) -> bool:
        try:
            subprocess.run(
                ["/usr/bin/pinctrl", "get", str(self.reset_gpio)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            return True
        except Exception:
            return False

    def set_input_pullup(self, gpio: int) -> None:
        try:
            subprocess.run(
                ["/usr/bin/pinctrl", "set", str(gpio), "ip", "pu"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
        except Exception as e:
            self.state.append_status(f"Button GPIO{gpio} pull-up setup failed: {e}")

    def get_level(self, gpio: int) -> str:
        try:
            out = subprocess.check_output(
                ["/usr/bin/pinctrl", "get", str(gpio)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1,
            )
            # Example:
            # 18: ip    pu | hi // GPIO18 = input
            if "|" in out:
                return out.split("|", 1)[1].strip().split()[0]
        except Exception:
            pass
        return "?"

    def handle_release(self, gpio: int, duration: float) -> None:
        long_press = duration >= self.long_press_sec

        if gpio == self.reset_gpio:
            if long_press:
                self.state.append_status(f"GPIO{gpio} long press: restart service")
                self.on_restart()
            else:
                self.state.append_status(f"GPIO{gpio} short press: reset copy/buffer")
                self.on_reset()

        elif gpio == self.page_gpio:
            if long_press:
                self.state.append_status(f"GPIO{gpio} long press: freeze/unfreeze TFT")
                self.on_toggle_freeze()
            else:
                self.state.append_status(f"GPIO{gpio} short press: next TFT page")
                self.on_next_page()

    def loop(self) -> None:
        pins = (self.reset_gpio, self.page_gpio)

        while not self.stop_event.is_set():
            now = time.time()

            for gpio in pins:
                val = self.get_level(gpio)
                last = self._last_val.get(gpio)

                if val == last:
                    continue

                self._last_val[gpio] = val

                if val == "lo":
                    self._pressed_at[gpio] = now

                elif val == "hi":
                    pressed_at = self._pressed_at.pop(gpio, None)
                    if pressed_at is not None:
                        self.handle_release(gpio, now - pressed_at)

            time.sleep(self.poll_sec)

# XC9022 / GoodTFT 2.8" Button Mapping

This document records the confirmed button mapping for The Morse Whisperer running on a Raspberry Pi 4 with the Jaycar XC9022 / GoodTFT 2.8" LCD.

## Confirmed Button Map

Physical buttons are numbered left to right.

```text
Button 1 = GPIO23
Button 2 = GPIO22
Button 3 = GPIO27
Button 4 = GPIO18
```

## Current Functions

```text
Button 1 short press = next TFT page
Button 1 long press  = freeze / unfreeze TFT

Button 2 short press = manual tone scan
Button 2 long press  = restart decoder service

Button 3 short press = reset decoder/copy buffer
Button 3 long press  = full reset + restart

Button 4 short press = clear/reset decoder/copy buffer
Button 4 long press  = full reset + restart
```

## Important Discovery

Passive GPIO polling did not detect Buttons 2 and 3.

Initial read-only tests showed no changes on GPIO22 or GPIO27 because those pins were sitting low without pull-ups.

The buttons only became visible after configuring GPIO22 and GPIO27 as inputs with pull-ups using `RPi.GPIO`.

Example confirmed result:

```text
GPIO22_possible_SCAN 1->0 PRESSED
GPIO22_possible_SCAN 0->1 released

GPIO27_possible_RESET 1->0 PRESSED
GPIO27_possible_RESET 0->1 released
```

## Active-Low Behaviour

All buttons are active-low:

```text
released = 1
pressed  = 0
```

## Why RPi.GPIO Is Used

The working button sidecar uses `RPi.GPIO` because it can explicitly configure internal pull-ups:

```python
GPIO.setmode(GPIO.BCM)
GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
```

Using `pinctrl get` alone is not enough to discover these buttons.

## Safety Note

Some LCD-show / GoodTFT configurations may use GPIO22 or GPIO27 for display control signals.

On this tested unit, the four-button mapping above works. Do not assume it is safe for every screen variant. Test your exact hardware before enabling all four buttons.

#!/usr/bin/env python

import sys
import time
from webcam import webcam_snap
import RPi.GPIO as GPIO
from blinkstick import blinkstick

bstick = blinkstick.find_first()

if bstick is None:
    sys.exit("BlinkStick not found...")

if len(sys.argv) == 2:
	color = sys.argv[1]
else:
	color = 'red'

for _ in range(3):
    bstick.set_color(name=color)
    time.sleep(0.25)
    bstick.turn_off()
    time.sleep(0.25)

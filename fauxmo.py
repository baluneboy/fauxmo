#!/usr/bin/env python -B

"""
The MIT License (MIT)

Copyright (c) 2015 Maker Musings

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# For some discussion, see http://www.makermusings.com

import email.utils
import requests
import select
import socket
import struct
import sys
import time
import urllib
import uuid
import datetime

from multiprocessing import Pool, TimeoutError

import os

from pims.wemocontrol import wemo_backend

import RPi.GPIO as GPIO

from blinkstick import blinkstick
bstick = blinkstick.find_first()
if bstick is None:
    sys.exit("BlinkStick not found...")


# This XML is the minimum needed to define one of our virtual switches
# to the Amazon Echo

SETUP_XML = """<?xml version="1.0"?>
<root>
  <device>
    <deviceType>urn:MakerMusings:device:controllee:1</deviceType>
    <friendlyName>%(device_name)s</friendlyName>
    <manufacturer>Belkin International Inc.</manufacturer>
    <modelName>Emulated Socket</modelName>
    <modelNumber>3.1415</modelNumber>
    <UDN>uuid:Socket-1_0-%(device_serial)s</UDN>
  </device>
</root>
"""


DEBUG = False
DRYRUN = True

def dbg(msg):
    global DEBUG
    if DEBUG:
        print datetime.datetime.now(), msg
        sys.stdout.flush()


def log(msg):
    dbg(msg)


def toggle_pinout(pinout=17, sec=1, dry_run=False):
    if dry_run:
        dbg('Need dry_run=False for toggle_pinout to actually work.')
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pinout, GPIO.OUT) 
    GPIO.output(pinout, GPIO.HIGH)
    dbg("GPIO (BCM) PINOUT %d PUSHED HIGH" % pinout)
    dbg('sleep for %d sec' % sec)
    time.sleep(sec)
    GPIO.output(pinout, GPIO.LOW)
    dbg("GPIO (BCM) PINOUT %d PULLED LOW" % pinout)
    GPIO.cleanup()  # Reset GPIO settings
    log("This will be the daily log message, so make it good.")


# A simple utility class to wait for incoming data to be
# ready on a socket.
class Poller(object):

    def __init__(self):
        if 'poll' in dir(select):
            self.use_poll = True
            self.poller = select.poll()
        else:
            self.use_poll = False
        self.targets = {}

    def add(self, target, fileno=None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.register(fileno, select.POLLIN)
        self.targets[fileno] = target

    def remove(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.unregister(fileno)
        del(self.targets[fileno])

    def poll(self, timeout = 0):
        if self.use_poll:
            ready = self.poller.poll(timeout)
        else:
            ready = []
            if len(self.targets) > 0:
                (rlist, wlist, xlist) = select.select(self.targets.keys(), [], [], timeout)
                ready = [(x, None) for x in rlist]
        for one_ready in ready:
            target = self.targets.get(one_ready[0], None)
            if target:
                target.do_read(one_ready[0])
 

# Base class for a generic UPnP device. This is far from complete
# but it supports either specified or automatic IP address and port
# selection.
class UpnpDevice(object):
    this_host_ip = None

    @staticmethod
    def local_ip_address():
        if not UpnpDevice.this_host_ip:
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp_socket.connect(('8.8.8.8', 53))
                UpnpDevice.this_host_ip = temp_socket.getsockname()[0]
            except:
                UpnpDevice.this_host_ip = '127.0.0.1'
            del(temp_socket)
            dbg("Got local address of %s" % UpnpDevice.this_host_ip)
        return UpnpDevice.this_host_ip

    def __init__(self, listener, poller, port, root_url, server_version, persistent_uuid, other_headers = None, ip_address = None):
        self.listener = listener
        self.poller = poller
        self.port = port
        self.root_url = root_url
        self.server_version = server_version
        self.persistent_uuid = persistent_uuid
        self.uuid = uuid.uuid4()
        self.other_headers = other_headers

        if ip_address:
            self.ip_address = ip_address
        else:
            self.ip_address = UpnpDevice.local_ip_address()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((self.ip_address, self.port))
        self.socket.listen(5)
        if self.port == 0:
            self.port = self.socket.getsockname()[1]
        self.poller.add(self)
        self.client_sockets = {}
        self.listener.add_device(self)

    def fileno(self):
        return self.socket.fileno()

    def do_read(self, fileno):
        if fileno == self.socket.fileno():
            (client_socket, client_address) = self.socket.accept()
            self.poller.add(self, client_socket.fileno())
            self.client_sockets[client_socket.fileno()] = client_socket
        else:
            data, sender = self.client_sockets[fileno].recvfrom(4096)
            if not data:
                self.poller.remove(self, fileno)
                del(self.client_sockets[fileno])
            else:
                self.handle_request(data, sender, self.client_sockets[fileno])

    def handle_request(self, data, sender, socket):
        pass

    def get_name(self):
        return "unknown"
        
    def respond_to_search(self, destination, search_target):
        dbg("Responding to search for %s" % self.get_name())
        date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
        location_url = self.root_url % {'ip_address' : self.ip_address, 'port' : self.port}
        message = ("HTTP/1.1 200 OK\r\n"
                  "CACHE-CONTROL: max-age=86400\r\n"
                  "DATE: %s\r\n"
                  "EXT:\r\n"
                  "LOCATION: %s\r\n"
                  "OPT: \"http://schemas.upnp.org/upnp/1/0/\"; ns=01\r\n"
                  "01-NLS: %s\r\n"
                  "SERVER: %s\r\n"
                  "ST: %s\r\n"
                  "USN: uuid:%s::%s\r\n" % (date_str, location_url, self.uuid, self.server_version, search_target, self.persistent_uuid, search_target))
        if self.other_headers:
            for header in self.other_headers:
                message += "%s\r\n" % header
        message += "\r\n"
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_socket.sendto(message, destination)
 

# This subclass does the bulk of the work to mimic a WeMo switch on
# the network.
class Fauxmo(UpnpDevice):

    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxmo!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler = None):
        self.serial = self.make_uuid(name)
        self.name = name
        self.ip_address = ip_address
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        UpnpDevice.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/setup.xml", "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, other_headers=other_headers, ip_address=ip_address)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        dbg("FauxMo device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def get_name(self):
        return self.name

    def handle_request(self, data, sender, socket):
        if data.find('GET /setup.xml HTTP/1.1') == 0:
            dbg("Responding to setup.xml for %s" % self.name)
            xml = SETUP_XML % {'device_name' : self.name, 'device_serial' : self.serial}
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml\r\n"
                       "DATE: %s\r\n"
                       "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(xml), date_str, xml))
            socket.send(message)
        elif data.find('SOAPACTION: "urn:Belkin:service:basicevent:1#SetBinaryState"') != -1:
            success = False
            if data.find('<BinaryState>1</BinaryState>') != -1:
                # on
                dbg("Responding to ON for %s" % self.name)
                success = self.action_handler.on()
            elif data.find('<BinaryState>0</BinaryState>') != -1:
                # off
                dbg("Responding to OFF for %s" % self.name)
                success = self.action_handler.off()
            else:
                dbg("Unknown Binary State request:")
                dbg(data)
            if success:
                # The echo is happy with the 200 status code and doesn't
                # appear to care about the SOAP response body
                soap = ""
                date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
                message = ("HTTP/1.1 200 OK\r\n"
                           "CONTENT-LENGTH: %d\r\n"
                           "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                           "DATE: %s\r\n"
                           "EXT:\r\n"
                           "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                           "X-User-Agent: redsonic\r\n"
                           "CONNECTION: close\r\n"
                           "\r\n"
                           "%s" % (len(soap), date_str, soap))
                socket.send(message)
        else:
            dbg(data)

    def on(self):
        return False

    def off(self):
        return True


# Since we have a single process managing several virtual UPnP devices,
# we only need a single listener for UPnP broadcasts. When a matching
# search is received, it causes each device instance to respond.
#
# Note that this is currently hard-coded to recognize only the search
# from the Amazon Echo for WeMo devices. In particular, it does not
# support the more common root device general search. The Echo
# doesn't search for root devices.
class UpnpBroadcastResponder(object):

    TIMEOUT = 0

    def __init__(self):
        self.devices = []

    def init_socket(self):
        ok = True
        self.ip = '239.255.255.250'
        self.port = 1900
        try:
            # This is needed to join a multicast group
            self.mreq = struct.pack("4sl",socket.inet_aton(self.ip),socket.INADDR_ANY)

            # Set up server socket
            self.ssock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
            self.ssock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)

            try:
                self.ssock.bind(('',self.port))
            except Exception, e:
                dbg("WARNING: Failed to bind %s:%d: %s" , (self.ip,self.port,e))
                ok = False

            try:
                self.ssock.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,self.mreq)
            except Exception, e:
                dbg('WARNING: Failed to join multicast group:',e)
                ok = False

        except Exception, e:
            dbg("Failed to initialize UPnP sockets:",e)
            return False
        if ok:
            dbg("Listening for UPnP broadcasts")

    def fileno(self):
        return self.ssock.fileno()

    def do_read(self, fileno):
        data, sender = self.recvfrom(1024)
        if data:
            if data.find('M-SEARCH') == 0 and data.find('urn:Belkin:device:**') != -1:
                for device in self.devices:
                    time.sleep(0.1)
                    device.respond_to_search(sender, 'urn:Belkin:device:**')
            else:
                pass

    # Receive network data
    def recvfrom(self,size):
        if self.TIMEOUT:
            self.ssock.setblocking(0)
            ready = select.select([self.ssock], [], [], self.TIMEOUT)[0]
        else:
            self.ssock.setblocking(1)
            ready = True

        try:
            if ready:
                return self.ssock.recvfrom(size)
            else:
                return False, False
        except Exception, e:
            dbg(e)
            return False, False

    def add_device(self, device):
        self.devices.append(device)
        dbg("UPnP broadcast listener: new device registered")


def is_time_between(now_time, start_time, end_time):
    """return True if given time (no date) is in between start & end times (end not included)"""
    if start_time <= end_time:
        return start_time <= now_time < end_time
    else:  # over midnight e.g., 23:30-04:15
        return start_time <= now_time or now_time < end_time


def is_weekday_work_time(start_time, end_time):
    """return True if today is weekday and time is between start_time & end_time"""
    b = False
    d = datetime.datetime.now()
    if d.isoweekday() in range(1, 6):
        b = is_time_between(d.time(), start_time, end_time)
    return b


# FIXME refactor this function with is_weekday_work_time
def is_sunday_mass_time(start_time, end_time):
    """return True if today is Sunday and time is between start_time & end_time"""
    b = False
    d = datetime.datetime.now()
    if d.isoweekday() == 7:
        b = is_time_between(d.time(), start_time, end_time)
    return b


def is_garage_open_time():
    """return True if it's day/time for work or for mass"""
    # define "go to work" time range (t1, t2) for a weekday
    t1 = datetime.datetime(2017, 8, 31, 5, 40, 0).time()  # only consider time part
    t2 = datetime.datetime(2017, 8, 31, 6, 50, 0).time()  # only consider time part

    # define "go to mass" time range (t1, t2) for a Sunday
    t3 = datetime.datetime(2017, 8, 31, 8, 00, 0).time()  # only consider time part
    t4 = datetime.datetime(2017, 8, 31, 9, 00, 0).time()  # only consider time part

    return is_weekday_work_time(t1, t2) or is_sunday_mass_time(t3, t4)


def sleep_and_wemo_off(sleep_sec, wemo_name):
    """if weekday_work or sunday_mass times, then turn off wemo device"""
    if is_garage_open_time():
        time.sleep(sleep_sec)  # wait several seconds to get downstairs & flip switch
        try:
            wemo_backend.wemo_dict[wemo_name].off()
            msg = 'slept for %d sec and then turned off the %s' % (sleep_sec, wemo_name)
        except ValueError:
            msg = 'slept for %d sec, but caught ValueError turning off the %s' % (sleep_sec, wemo_name)
    else:
        msg = 'did nothing for "sleep_and_wemo_off" because it is not one of those days/times'
    return msg


def sleep_camsnap_torchoff(sleep_sec, cam_label, cam_dtm, wemo_name):
    """if weekday_work or sunday_mass times, then turn off wemo device"""
    if is_garage_open_time():
        time.sleep(sleep_sec)  # wait several seconds to get downstairs & flip switch
        webcam_snap(cam_label, cam_dtm)
        try:
            wemo_backend.wemo_dict[wemo_name].off()
            msg = 'slept for %d sec, snapped pic, then turned off the %s' % (sleep_sec, wemo_name)
        except ValueError:
            msg = 'slept for %d sec, but caught ValueError turning off the %s' % (sleep_sec, wemo_name)
    else:
        msg = 'did nothing for "sleep_camsnap_torchoff" because it is not one of those days/times'
    return msg


def just_squawk(s):
    """for multiprocessing, a callback that shows what's returned from a func eval
    [ whenever that occurs asynchronously ] -- the func here is sleep_and_wemo_off
    """
    dbg('The just_squawk callback function %s.' % s)


# This is an example handler class. The Fauxmo class expects handlers to be
# instances of objects that have on() and off() methods that return True
# on success and False otherwise.
#
# This example class takes two full URLs that should be requested when an on
# and off command are invoked respectively. It ignores any return data.
class RestApiHandler(object):

    def __init__(self, on_cmd, off_cmd, on_color='green', off_color='red'):
        self.on_cmd = on_cmd
        self.off_cmd = off_cmd
        self.on_color = on_color
        self.off_color = off_color
        self._pool = Pool(processes=3)  # start 3 worker processes

    def on(self):
        dbg("The on_cmd received by %s" % self.__class__.__name__)
        for _ in range(3):
            bstick.set_color(name=self.on_color)
            time.sleep(0.25)
            bstick.turn_off()
            time.sleep(0.25)
        return True

    def off(self):
        dbg("The off_cmd received by %s" % self.__class__.__name__)
        for _ in range(3):
            bstick.set_color(name=self.off_color)
            time.sleep(0.25)
            bstick.turn_off()
            time.sleep(0.25)
        return True


class GarageRestApiHandler(RestApiHandler):

    def on(self):
        """Turning 'on' the garage means open it."""
        
        dbg("The on_cmd received by %s" % self.__class__.__name__)
        for _ in range(3):
            bstick.set_color(name=self.on_color)
            time.sleep(0.250)
            bstick.turn_off()
            time.sleep(0.250)

        # ftw
        dtm = datetime.datetime.now()
        webcam_snap('close', dtm)  # label this pic as 'close' since expecting garage is closed
        toggle_pinout(dry_run=DRYRUN)  # raspberry pi hack to, in effect, push garage door button via relay

        # depending on day of week and time of day, we push garage door remote button...and
        # use multiprocessing async to do "sleep and torch off" so Alexa does not timeout
        #self._pool.apply_async(sleep_and_wemo_off, (20, 'torch'), callback=just_squawk)
        self._pool.apply_async(sleep_camsnap_torchoff, (20, 'open', dtm, 'torch'), callback=just_squawk)
        dbg('Delayed multiprocessing being done async now so Alexa does not timeout')

        # return True is expected
        return True

    def off(self):
        """Turning 'off' the garage means close it."""
        
        dbg("The off_cmd received by %s" % self.__class__.__name__)
        for _ in range(3):
            bstick.set_color(name=self.off_color)
            time.sleep(0.250)
            bstick.turn_off()
            time.sleep(0.250)

        # ftw
        dtm = datetime.datetime.now()
        webcam_snap('open', dtm)  # label this pic as 'open' since expecting garage is opened
        toggle_pinout(dry_run=DRYRUN)  # raspberry pi hack to, in effect, push garage door button via relay

        # depending on day of week and time of day, we push garage door remote button...and
        # use multiprocessing async to do "sleep and torch off" so Alexa does not timeout
        #self._pool.apply_async(sleep_and_wemo_off, (20, 'torch'), callback=just_squawk)
        self._pool.apply_async(sleep_camsnap_torchoff, (20, 'close', dtm, 'torch'), callback=just_squawk)
        dbg('Delayed multiprocessing being done async now so Alexa does not timeout')

        # return True is expected
        return True


# Each entry is a list with the following elements:
#
# name of the virtual switch
# object with 'on' and 'off' methods
# port # (optional; may be omitted)

# NOTE: As of 2015-08-17, the Echo appears to have a hard-coded limit of
# 16 switches it can control. Only the first 16 elements of the FAUXMOS
# list will be used.
FAUXMOS = [
    ['office lights', RestApiHandler('http://192.168.1.109/ha-api?cmd=on&a=office', 'http://192.168.1.109/ha-api?cmd=off&a=office', on_color='cyan', off_color='magenta')],
    ['kitchen lights', RestApiHandler('http://192.168.1.109/ha-api?cmd=on&a=kitchen', 'http://192.168.1.109/ha-api?cmd=off&a=kitchen', on_color='orange', off_color='blue')],
    ['garage door', GarageRestApiHandler('http://192.168.1.109/ha-api?cmd=on&a=garage', 'http://192.168.1.109/ha-api?cmd=off&a=garage', on_color='green', off_color='red')],
]


if len(sys.argv) > 1 and sys.argv[1] == '-d':
    DEBUG = True

# Set up our singleton for polling the sockets for data ready
p = Poller()

# Set up our singleton listener for UPnP broadcasts
u = UpnpBroadcastResponder()
u.init_socket()

# Add the UPnP broadcast listener to the Poller so we can respond
# when a broadcast is received.
p.add(u)

# Create our FauxMo virtual switch devices
for one_faux in FAUXMOS:
    if len(one_faux) == 2:
        # a fixed port wasn't specified, use a dynamic one
        one_faux.append(0)
    switch = Fauxmo(one_faux[0], u, p, None, one_faux[2], action_handler = one_faux[1])

dbg("Entering main loop\n")

while True:
    try:
        # Allow time for a ctrl-c to stop the process
        p.poll(100)
        time.sleep(0.1)
    except Exception, e:
        dbg(e)
        break


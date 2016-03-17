#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# License under MIT

from __future__ import absolute_import

import collections
import json
import os
import platform
import re
import sys
import subprocess
import time
import tempfile
import threading
import warnings
import logging
import xml.dom.minidom

import cv2
import numpy as np
import aircv as ac
from uiautomator import device as d
from uiautomator import Device as UiaDevice
from uiautomator import AutomatorDeviceObject
from PIL import Image

from atx import consts
from atx import errors
from atx import patch
from atx import base
from atx import logutils
from atx import imutils


log = logutils.getLogger(__name__)
log.setLevel(logging.DEBUG)
FindPoint = collections.namedtuple('FindPoint', ['pos', 'confidence', 'method', 'matched'])
UINode = collections.namedtuple('UINode', ['bounds', 'checkable', 'class_name', 'text', 'resource_id', 'package'])
Bounds = collections.namedtuple('Bounds', ['left', 'top', 'right', 'bottom'])

__dir__ = os.path.dirname(os.path.abspath(__file__))
__tmp__ = os.path.join(__dir__, '__cache__')

DISPLAY_RE = re.compile(
    '.*DisplayViewport{valid=true, .*orientation=(?P<orientation>\d+), .*deviceWidth=(?P<width>\d+), deviceHeight=(?P<height>\d+).*')


class Pattern(object):
    def __init__(self, image, offset=(0, 0), anchor=0):
        self._name = None
        self._image = imutils.open(image)
        self._offset = offset
        
        if isinstance(image, basestring):
            self._name = image

    def __str__(self):
        return 'Pattern(name: {}, offset: {})'.format(self._name, self.offset)
    
    @property
    def image(self):
        return self._image

    @property
    def offset(self):
        return self._offset
    
    
class Watcher(object):
    ACTION_CLICK = 1 <<0
    ACTION_TOUCH = 1 <<0
    ACTION_QUIT = 1 <<1

    Event = collections.namedtuple('WatchEvent', ['selector', 'actions'])

    def __init__(self, device, name=None, timeout=None):
        self._events = []
        self._dev = device
        self._run = False
        self._stored_selector = None

        self.name = name
        self.touched = {}
        self.timeout = timeout

    def on(self, image=None, actions=None, text=None):
        """Trigger when some object exists
        Args:
            image: string location of an image
            flags: ACTION_TOUCH | ACTION_QUIT

        Returns:
            None
        """
        if isinstance(image, basestring):
            self._stored_selector = Pattern(image)
        elif text:
            self._stored_selector = self._dev(text=text)

        if actions:
            self._events.append(Watcher.Event(image, actions))
            self._stored_selector = None
        return self

    def touch(self):
        return self.click()

    def click(self):
        self._events.append(Watcher.Event(self._stored_selector, Watcher.ACTION_CLICK))
        return self

    def quit(self):
        self._events.append(Watcher.Event(self._stored_selector, Watcher.ACTION_QUIT))

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self._run_watch()

    def _match(self, selector, screen):
        ''' returns position(x, y) or None'''
        if isinstance(selector, Pattern):
            ret = self._dev.exists(selector.image, screen=screen)
            if ret is None:
                return None
            return ret.pos

            # exists = False
            # if ret.method == consts.IMAGE_MATCH_METHOD_TMPL:
            #     if ret.confidence > 0.8:
            #         exists = True
            #     # else:
            #         # print("Skip confidence:", ret.confidence)
            # elif ret.method == consts.IMAGE_MATCH_METHOD_SIFT:
            #     matches, total = ret.confidence
            #     if 1.0*matches/total > 0.5:
            #         exists = True

            # if exists:
        elif isinstance(selector, AutomatorDeviceObject):
            if not selector.exists:
                return None
            info = selector.info['bounds']
            x = (info['left'] + info['right']) / 2
            y = (info['bottom'] + info['top']) / 2
            return (x, y)

    def _hook(self, screen):
        for evt in self._events:
            pos = self._match(evt.selector, screen)
            if pos is None:
                continue

            if evt.actions & Watcher.ACTION_CLICK:
                log.debug('trigger watch click: %s', pos)
                self._dev.click(*pos)

            if evt.actions & Watcher.ACTION_QUIT:
                self._run = False

    def _run_watch(self):
        self._run = True
        start_time = time.time()
        
        while self._run:
            screen = self._dev.screenshot()
            self._hook(screen)
            if self.timeout is not None:
                if time.time() - start_time > self.timeout:
                    raise errors.WatchTimeoutError("Watcher(%s) timeout %s" % (self.name, self.timeout,))
                sys.stdout.write("Watching %4.1fs left: %4.1fs\r" %(self.timeout, self.timeout-time.time()+start_time))
                #log.debug("Watching %4.1fs left: %4.1fs\r" %(self.timeout, self.timeout-time.time()+start_time))
                sys.stdout.flush()
        sys.stdout.write('\n')

def remove_force(name):
    if os.path.isfile(name):
        os.remove(name)

class DeviceMixin(object):
    def __init__(self):
        self.image_match_method = consts.IMAGE_MATCH_METHOD_TMPL
        self.resolution = None
        self.image_match_threshold = 0.8
        self._bounds = None

    def sleep(self, secs):
        """Sleep some seconds

        Args:
            secs: float seconds

        Returns:
            self
        """
        secs = int(secs)
        for i in reversed(range(secs)):
            sys.stdout.write('\r')
            sys.stdout.write("sleep %ds, left %2ds" % (secs, i+1))
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\n")
        return self

    def exists(self, img, screen=None):
        """Check if image exists in screen

        Returns:
            If exists, return FindPoint, or
            return None if result.confidence < self.image_match_threshold
        """
        ret = self.match(img, screen)
        if ret is None:
            return None
        if not ret.matched:
            return None
        return ret

    def match(self, pattern, screen=None, threshold=None):
        """Check if image position in screen

        Args:
            pattern: Image file name or opencv image object
            screen: opencv image, optional, if not None, screenshot method will be called

        Returns:
            None or FindPoint, For example:

            FindPoint(pos=(20, 30), method='tmpl', confidence=0.801, matched=True)

            Only when confidence > self.image_match_threshold, matched will be True

        Raises:
            SyntaxError: when image_match_method is invalid
        """
        if not isinstance(pattern, Pattern):
            pattern = Pattern(pattern)
        # search_img = imutils.open(img)
        search_img = pattern.image
        if screen is None:
            screen = self.screenshot()
        if threshold is None:
            threshold = self.image_match_threshold

        dx, dy = pattern.offset
        # image match
        screen = imutils.from_pillow(screen) # convert to opencv image
        if self.image_match_method == consts.IMAGE_MATCH_METHOD_TMPL:
            if self.resolution is not None:
                ow, oh = self.resolution
                fx, fy = 1.0*self.display.width/ow, 1.0*self.display.height/oh
                # For horizontal screen, scale by Y
                # For vertical screen, scale by X
                # Offset scale by X and Y
                # FIXME(ssx): need test here, I never tried before.
                scale = fy if self.rotation in (1, 3) else fx
                search_img = cv2.resize(search_img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                dx, dy = int(dx*fx), int(dy*fy)

                # TODO(useless)
                # cv2.imwrite('resize-tmp.png', search_img)
                # cv2.imwrite('current-screen.png', screen)
            ret = ac.find_template(screen, search_img)
        elif self.image_match_method == consts.IMAGE_MATCH_METHOD_SIFT:
            ret = ac.find_sift(screen, search_img, min_match_count=10)
        else:
            raise SyntaxError("Invalid image match method: %s" %(self.image_match_method,))

        if ret is None:
            return None
        (x, y) = ret['result']
        # fix by offset
        position = (x+dx, y+dy)
        confidence = ret['confidence']

        matched = True
        if self.image_match_method == consts.IMAGE_MATCH_METHOD_TMPL:
            if confidence < self.image_match_threshold:
                matched = False
        elif self.image_match_method == consts.IMAGE_MATCH_METHOD_SIFT:
            matches, total = confidence
            if 1.0*matches/total > 0.5: # FIXME(ssx): sift just write here
                matched = True
        return FindPoint(position, confidence, self.image_match_method, matched=matched)

    def region(self):
        """TODO"""
        return self

    def touch_image(self, *args, **kwargs):
        """ALias for click_image"""
        self.click_image(*args, **kwargs)

    def click_image(self, img, timeout=20.0, wait_change=False):
        """Simulate click according image position

        Args:
            img: filename or an opencv image object
            timeout: float, if image not found during this time, ImageNotFoundError will raise.
            wait_change: wait until background image changed.
        Returns:
            None

        Raises:
            ImageNotFoundError: An error occured when img not found in current screen.
        """
        search_img = imutils.open(img)
        log.info('touch image: %s', img)
        start_time = time.time()
        found = False
        while time.time() - start_time < timeout:
            point = self.match(search_img)
            if point is None:
                sys.stdout.write('.')
                sys.stdout.flush()
                continue
            if not point.matched:
                log.debug('Ignore confidence: %s', point.confidence)
                continue
            log.debug('confidence: %s', point.confidence)
            self.touch(*point.pos)
            found = True
            break
        sys.stdout.write('\n')

        # wait until click area not same
        if found and wait_change:
            start_time = time.time()
            while time.time()-start_time < timeout:
                # screen_img = self.screenshot()
                ret = self.match(search_img)
                if ret is None:
                    break

        if not found:
            raise errors.ImageNotFoundError('Not found image %s' %(img,))

    def watch(self, name, timeout=None):
        """Return a new watcher
        Args:
            name: string watcher name
            timeout: watch timeout

        Returns:
            watcher object
        """
        w = Watcher(self, name, timeout)
        w._dev = self
        return w

class AndroidDevice(DeviceMixin, UiaDevice):
    def __init__(self, serialno=None, **kwargs):
        self._host = kwargs.get('host', '127.0.0.1')
        self._port = kwargs.get('port', 5037)

        kwargs['adb_server_host'] = kwargs.pop('host', self._host)
        kwargs['adb_server_port'] = kwargs.pop('port', self._port)
        UiaDevice.__init__(self, serialno, **kwargs)
        DeviceMixin.__init__(self)

        self._randid = base.id_generator(5)
        self._serial = serialno
        self._uiauto = super(AndroidDevice, self)

        self.screen_rotation = None
        self.screenshot_method = consts.SCREENSHOT_METHOD_AUTO
        self.last_screenshot = None

    @property
    def wlan_ip(self):
        """ Wlan IP """
        return self.adb_shell(['getprop', 'dhcp.wlan0.ipaddress']).strip()

    def is_app_alive(self, package_name):
        """ Check if app in running in foreground """
        return d.info['currentPackageName'] == package_name

    @property
    @patch.run_once
    def display(self):
        """Virtual keyborad may get small d.info['displayHeight']
        """
        w, h = (0, 0)
        for line in self.adb_shell('dumpsys display').splitlines():
            m = DISPLAY_RE.search(line, 0)
            if not m:
                continue
            w = int(m.group('width'))
            h = int(m.group('height'))
            # o = int(m.group('orientation'))
            w, h = min(w, h), max(w, h)
            return collections.namedtuple('Display', ['width', 'height'])(w, h)

        w, h = d.info['displayWidth'], d.info['displayHeight']
        w, h = min(w, h), max(w, h)
        return collections.namedtuple('Display', ['width', 'height'])(w, h)
    
    @property
    def rotation(self):
        """
        Rotaion of the phone

        0: normal
        1: power key on the right
        2: power key on the top
        3: power key on the left
        """
        if self.screen_rotation in range(4):
            return self.screen_rotation
        return self.info['displayRotation']
    
    def _minicap_params(self):
        """
        Used about 0.1s
        uiautomator d.info is now well working with device which has virtual menu.
        """
        rotation = self.screen_rotation 
        if self.screen_rotation is None:
            rotation = self.rotation

        # rotation not working on SumSUNG 9502
        return '{x}x{y}@{x}x{y}/{r}'.format(
            x=self.display.width,
            y=self.display.height,
            r=rotation*90)
    
    def _screenshot_minicap(self):
        phone_tmp_file = '/data/local/tmp/_atx_screen-{}.jpg'.format(self._randid)
        local_tmp_file = tempfile.mktemp(prefix='atx-tmp-', suffix='.jpg')
        command = 'LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap -P {} -s > {}'.format(
            self._minicap_params(), phone_tmp_file)
        try:
            self.adb_shell(command)
            self.adb(['pull', phone_tmp_file, local_tmp_file])
            image = imutils.open_as_pillow(local_tmp_file)

            # Fix rotation not rotate right.
            (width, height) = image.size
            if self.screen_rotation in [1, 3] and width < height:
                image = image.rotate(90, Image.BILINEAR, expand=True)
            return image
        except IOError:
            raise IOError("Screenshot use minicap failed.")
        finally:
            # remove_force(local_tmp_file)
            self.adb_shell(['rm', phone_tmp_file])

    def _screenshot_uiauto(self):
        tmp_file = tempfile.mktemp(prefix='atx-tmp-', suffix='.jpg')
        self._uiauto.screenshot(tmp_file)
        try:
            return imutils.open_as_pillow(tmp_file)
        except IOError:
            raise IOError("Screenshot use uiautomator failed.")
        finally:
            remove_force(tmp_file)

    def touch(self, x, y):
        """ Alias for click """
        self.click(x, y)

    def click(self, x, y):
        """
        Touch specify position

        Args:
            x, y: int

        Returns:
            None
        """
        return self._uiauto.click(x, y)

    def screenshot(self, filename=None):
        """
        Take screen snapshot

        Args:
            filename: filename where save to, optional

        Returns:
            PIL.Image object

        Raises:
            TypeError, IOError
        """
        screen = None
        if self.screenshot_method == consts.SCREENSHOT_METHOD_UIAUTOMATOR:
            screen = self._screenshot_uiauto()
        elif self.screenshot_method == consts.SCREENSHOT_METHOD_MINICAP:
            screen = self._screenshot_minicap()
        elif self.screenshot_method == consts.SCREENSHOT_METHOD_AUTO:
            try:
                screen = self._screenshot_minicap()
                self.screenshot_method = consts.SCREENSHOT_METHOD_MINICAP
            except IOError:
                screen = self._screenshot_uiauto()
                self.screenshot_method = consts.SCREENSHOT_METHOD_UIAUTOMATOR
        else:
            raise TypeError('Invalid screenshot_method')

        if filename:
            save_dir = os.path.dirname(filename) or '.'
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            screen.save(filename)

        self.last_screenshot = screen
        return screen

    def adb(self, command):
        '''
        Run adb command, for example: adb(['pull', '/data/local/tmp/a.png'])

        Args:
            command: string or list of string

        Returns:
            command output
        '''
        cmds = ['adb']
        if self._serial:
            cmds.extend(['-s', self._serial])
        cmds.extend(['-H', self._host, '-P', str(self._port)])

        if isinstance(command, list) or isinstance(command, tuple):
            cmds.extend(list(command))
        else:
            cmds.append(command)
        # print cmds
        output = subprocess.check_output(cmds, stderr=subprocess.STDOUT)
        return output.replace('\r\n', '\n')

    def adb_shell(self, command):
        '''
        Run adb shell command

        Args:
            command: string or list of string

        Returns:
            command output
        '''
        if isinstance(command, list) or isinstance(command, tuple):
            return self.adb(['shell'] + list(command))
        else:
            return self.adb(['shell'] + [command])

    def start_app(self, package_name):
        '''
        Start application

        Args:
            package_name: string like com.example.app1

        Returns:
            None
        '''
        self.adb_shell(['monkey', '-p', package_name, '-c', 'android.intent.category.LAUNCHER', '1'])
        return self

    def stop_app(self, package_name, clear=False):
        '''
        Stop application

        Args:
            package_name: string like com.example.app1
            clear: bool, remove user data

        Returns:
            None
        '''
        if clear:
            self.adb_shell(['pm', 'clear', package_name])
        else:
            self.adb_shell(['am', 'force-stop', package_name])
        return self

    def takeSnapshot(self, filename):
        '''
        Deprecated, use screenshot instead.
        '''
        warnings.warn("deprecated, use snapshot instead", DeprecationWarning)
        return self.screenshot(filename)

    def _parse_xml_node(self, node):
        # ['bounds', 'checkable', 'class', 'text', 'resource_id', 'package']
        __alias = {
            'class': 'class_name',
            'resource-id': 'resource_id',
        }

        def parse_bounds(text):
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', text)
            if m is None:
                return None
            return Bounds(*map(int, m.groups()))

        def str2bool(v):
            return v.lower() in ("yes", "true", "t", "1")

        def convstr(v):
            return v.encode('utf-8')

        parsers = {
            'bounds': parse_bounds,
            'text': convstr,
            'class_name': convstr,
            'checkable': str2bool,
            'resource_id': convstr,
            'package': convstr,
        }
        ks = {}
        for key, value in node.attributes.items():
            key = __alias.get(key, key)
            f = parsers.get(key)
            if f:
                ks[key] = f(value)
        return UINode(**ks)

    def dump_nodes(self):
        """Dump current screen UI to list
        Returns:
            List of UINode object, For
            example:

            [UINode(
                bounds=Bounds(left=0, top=0, right=480, bottom=168),
                checkable=False,
                class_name='android.view.View',
                text='',
                resource_id='',
                package='com.sonyericsson.advancedwidget.clock')]
        """
        xmldata = self._uiauto.dump()
        dom = xml.dom.minidom.parseString(xmldata.encode('utf-8'))
        root = dom.documentElement
        nodes = root.getElementsByTagName('node')
        ui_nodes = []
        for node in nodes:
            ui_nodes.append(self._parse_xml_node(node))
        return ui_nodes

    # def safe_wait(self, img, seconds=20.0):
    #     '''
    #     Like wait, but don't raise RuntimeError

    #     return None when timeout
    #     return point if found
    #     '''
    #     warnings.warn("deprecated, use safe_wait instead", DeprecationWarning)
    #     try:
    #         return self.wait(img, seconds)
    #     except:
    #         return None        

    # def type(self, text):
    #     '''
    #     Input some text

    #     @param text: string (text want to type)
    #     '''
    #     self.dev.type(text)

    # def keyevent(self, event):
    #     '''
    #     Send keyevent (only support android and ios)

    #     @param event: string (one of MENU,BACK,HOME)
    #     @return nothing
    #     '''
    #     if hasattr(self.dev, 'keyevent'):
    #         return self.dev.keyevent(event)
    #     raise RuntimeError('keyevent not support')
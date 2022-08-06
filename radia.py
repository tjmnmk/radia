#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import os
import logging
import time
import subprocess
import json
import spidev
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont
from dataclasses import dataclass
import vlc

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
STATIONS_LIST = "CZ.json"
STATIONS_LIST_BACKUP = "CZ_bak.json"

logging.basicConfig(level=logging.DEBUG)
LOGGER = logging.getLogger(__name__)


class SH1106:
    def __init__(self):
        spi = spidev.SpiDev(0, 0)
        spi.max_speed_hz = 2000000
        spi.mode = 0
        self._spi = spi

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.RESET_PIN, GPIO.OUT)
        GPIO.setup(self.DC_PIN, GPIO.OUT)
        GPIO.setup(self.CS_PIN, GPIO.OUT)
        GPIO.setup(self.BL_PIN, GPIO.OUT)

        GPIO.output(self.CS_PIN, 0)
        GPIO.output(self.BL_PIN, 1)
        GPIO.output(self.DC_PIN, 0)

        self.reset()
        self._run_commands(self.INIT_COMMANDS)
        time.sleep(0.1)
        self._run_command(0xAF)

    def _run_command(self, command):
        GPIO.output(self.DC_PIN, GPIO.LOW)
        self._spi.writebytes((command,))

    def _run_commands(self, commands):
        for command in commands:
            self._run_command(command)

    def reset(self):
        GPIO.output(self.RESET_PIN, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(self.RESET_PIN, GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(self.RESET_PIN, GPIO.HIGH)
        time.sleep(0.1)

    def display_image(self, pil_image, invert = True):
        buf_size = self.HEIGHT_RES * self.WIDTH_RES // 8
        buf = [0xFF] * buf_size
        image_raw_pixels = pil_image.convert('1').load()
        for y in range(self.HEIGHT_RES):
            for x in range(self.WIDTH_RES):
                if image_raw_pixels[x, y] == 0:
                    buf[x + (y // 8) * self.WIDTH_RES] &= ~(1 << (y % 8))

        for page in range(0, self.HEIGHT_RES // 8):
            self._run_command(0xB0 + page)
            self._run_command(0x02)
            self._run_command(0x10)
            time.sleep(0.01)
            GPIO.output(self.DC_PIN, GPIO.HIGH)

            for i in range(0, self.WIDTH_RES):
                page_data = buf[i + self.WIDTH_RES * page]
                if not invert:
                    page_data = ~page_data
                self._spi.writebytes((~page_data,))

    HEIGHT_RES = 64
    WIDTH_RES = 128
    RESET_PIN       = 25
    DC_PIN          = 24
    CS_PIN          = 8
    BL_PIN          = 18
    INIT_COMMANDS   = (0xAE,
    0x02,
    0x10,
    0x40,
    0x81,
    0xA0,
    0xC0,
    0xA6,
    0xA8,
    0x3F,
    0xD3,
    0x00,
    0xD5,
    0x80,
    0xD9,
    0xF1,
    0xDA,
    0x12,
    0xDB,
    0x40,
    0x20,
    0x02,
    0xA4,
    0xA6,
    )


class NoStation(Exception):
    pass


@dataclass
class Station:
    id: int
    name: str
    stream : str


class State:
    def __init__(self):
        self._station_playing = None
        self._stations = {}
        self._station_select = 0
        self._player = None
        self._vlc_instance = None
        self._vlc_media = None

        try:
            self._load_stations(STATIONS_LIST)
        except Exception as e:
            self._load_stations(STATIONS_LIST_BACKUP)
        else:
            if len(self._stations) == 0:
                self._load_stations(STATIONS_LIST_BACKUP)

    def _load_stations(self, station_file):
        self._stations = {}
        try:
            with open(station_file, "r") as f:
                data = json.load(f)["stanice"]
                for station in data:
                    self._stations[station["id"]] = Station(int(station["id"]), station["nazov"], station["url"])
        except Exception as e:
            self._stations = {}
            raise

    def get_station_select(self):
        return self._station_select

    def set_station_select_next(self):
        if self._station_select == len(self.station_names()) - 1:
            return False
        self._station_select += 1
        return True

    def set_station_select_prev(self):
        if self._station_select == 0:
            return False
        self._station_select -= 1
        return True

    def station_names(self):
        stations = []
        for id, station in self._stations.items():
            stations.append(station.name)
        stations.sort()
        return stations

    def station_playing_name(self):
        if self._station_playing == None:
            return None
        return self._station_playing.name

    def play_stop(self):
        if self._player != None:
            self._player.stop()
            self._player.release()
        if self._vlc_media != None:
            self._vlc_media.release()
        if self._vlc_instance != None:
            self._vlc_instance.release()
        self._vlc_instance = None
        self._player = None
        self._vlc_media = None
        self._station_playing = None

    def play_station_by_name(self, name):
        station = None
        for id, stationc in self._stations.items():
            if name == stationc.name:
                station = stationc
                break
        if station == None:
            raise NoStation
        LOGGER.debug("Playing: %s", station.stream)
        vlc_instance = vlc.Instance()
        vlc_media = vlc_instance.media_new(station.stream, "network-caching=7000")
        player = vlc_instance.media_player_new()
        player.set_media(vlc_media)
        player.play()
        self._player = player
        self._vlc_instance = vlc_instance
        self._vlc_media = vlc_media
        self._station_playing = station

    def play_selected_station(self):
        self.play_stop()
        station_name = self.station_names()[self._station_select]
        self.play_station_by_name(station_name)

    def shutdown(self):
        os.system('sudo shutdown -h now')


class Display:
    def __init__(self):
        disp = SH1106()

        self._disp = disp
        self._font = ImageFont.truetype(FONT, 13)
        self._font_hdd = ImageFont.truetype(FONT, 52)


    def refresh(self, state):
        image = Image.new('1', (self._disp.WIDTH_RES, self._disp.HEIGHT_RES), "WHITE")
        draw = ImageDraw.Draw(image)

        station_choice = ["", "", ""]
        station_select = state.get_station_select()
        station_names = state.station_names()

        if len(station_names) == 0:
            station_choice[1] = "ERROR"
        else:
            station_choice[1] = ">" + station_names[station_select]
            try:
                if station_select > 0:
                    station_choice[0] = " " + station_names[station_select - 1]
            except IndexError:
                pass
            try:
                station_choice[2] = " " + station_names[station_select + 1]
            except IndexError:
                pass

        playing_name = state.station_playing_name()
        if playing_name:
            draw.text((0,0), "• " + playing_name, font = self._font)
        draw.text((0,15), station_choice[0], font = self._font)
        draw.text((0,30), station_choice[1], font = self._font)
        draw.text((0,45), station_choice[2], font = self._font)
        self._disp.display_image(image)

    def clear(self):
        image = Image.new('1', (self._disp.WIDTH_RES, self._disp.HEIGHT_RES), "WHITE")
        self._disp.display_image(image)


class WVSButtons:
    def __init__(self):
        self._button_last_time = 0
        self._button_last = 0
        for pin in self.PINS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def wait_on_button(self):
        while True:
            time.sleep(0.01)
            for pin in self.PINS:
                if not GPIO.input(pin):
                    button_time_d = time.time() - self._button_last_time
                    if self._button_last == pin and button_time_d > 0 and button_time_d < 0.1:
                        continue
                    self._button_last_time = time.time()
                    self._button_last = pin
                    try:
                        return self.BUTTON_NAMES[pin]
                    except KeyError:
                        pass

    KEY1 = 21
    KEY2 = 20
    KEY3 = 16
    J_UP = 6
    J_DOWN = 19
    J_LEFT = 5
    J_RIGHT = 26
    J_PRESS = 13
    PINS = (KEY1,
    KEY2,
    KEY3,
    J_UP,
    J_DOWN,
    J_LEFT,
    J_RIGHT,
    J_PRESS,
    )
    BUTTON_NAMES = {
        KEY1 : "key1",
        KEY2 : "key2",
        KEY3 : "key3",
        J_UP : "up",
        J_DOWN : "down",
    }


class Main:
    def __init__(self):
        self._state = State()
        self._display = Display()
        self._display.clear()
        self._display.refresh(self._state)
        self._buttons = WVSButtons()

        self._BUTTON_FUNC = {
            "up" : self._button_up,
            "down" : self._button_down,
            "key1" : self._button_play,
            "key2" : self._button_stop,
            "key3" : self._button_shutdown,
        }

    def main(self):
        self._state.play_station_by_name("Rádio Beat")
        self._display.refresh(self._state)
        try:
            while True:
                button = self._buttons.wait_on_button()
                try:
                    f = self._BUTTON_FUNC[button]
                except KeyError:
                    pass
                LOGGER.debug("Pressed %s", button)
                f()
                self._display.refresh(self._state)
        finally:
            self._display.clear()

    def _button_up(self):
        self._state.set_station_select_prev()

    def _button_down(self):
        self._state.set_station_select_next()

    def _button_play(self):
        self._state.play_selected_station()

    def _button_stop(self):
        self._state.play_stop()

    def _button_shutdown(self):
        self._state.shutdown()


if __name__ == "__main__":
    Main().main()


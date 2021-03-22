import io
import os
import re
import struct
import sublime
import sublime_plugin
import urllib.request

from base64 import b64encode, b64decode
from coloraide import Color
from functools import lru_cache
from socket import timeout
from .lib import png


SETTINGS_FILE = 'QuickView.sublime-settings'

EM_SCALE_FACTOR = 1/8.4  # this means the following pixel values correspond to a layout with view.em_width() == 8.4
MIN_POPUP_IMAGE_WIDTH = 100
MAX_POPUP_IMAGE_WIDTH = 200

SUPPORTED_PROTOCOLS = ('http:', 'https:', 'ftp:')
SUPPORTED_MIME_TYPES = ('image/bmp', 'image/gif', 'image/jpeg', 'image/png')
SUPPORTED_FILE_EXTENSIONS = ('bmp', 'gif', 'jpg', 'jpeg', 'png')  # @todo Add support for svg and webp images if possible without binary dependency

COLOR_PATTERN = re.compile(r'(?i)(?:\b(?<![-#&$])(?:color|hsla?|lch|lab|hwb|rgba?)\([^)]+\))')

flags = sublime.HIDE_ON_MOUSE_MOVE_AWAY

quickview_template = '''
    <body id="quick-view">
        <style>
            html.light {{
                --bc-highlight:         rgba(255, 255, 255, 0.12);
                --bc-highlight-bottom:  color(var(--background) lightness(- 7%));
                --bc-panel-bg:          color(var(--background) lightness(- 9%));
                --bc-panel-bg-promoted: color(var(--background) lightness(- 13.5%));
                --bc-text:              color(var(--foreground) lightness(- 12.5%));
            }}
            html.dark {{
                --bc-highlight:         rgba(255, 255, 255, 0.06);
                --bc-highlight-bottom:  color(var(--background) lightness(+ 9.8%));
                --bc-panel-bg:          color(var(--background) lightness(+ 5%));
                --bc-panel-bg-promoted: color(var(--background) lightness(+ 1%));
                --bc-text:              color(var(--foreground) lightness(- 6.5%));
            }}
            body {{
                padding: 0;
                margin: 0;
                background-color: var(--background);
            }}
            .preview-bubble {{
                width: 0;
                height: 0;
                margin-left: {margin}px;
                border-left: 0.65rem solid transparent;
                border-right: 0.65rem solid transparent;
            }}
            .bubble-below {{
                border-top: 0.65rem solid var(--bc-panel-bg);
            }}
            .bubble-above {{
                border-bottom: 0.65rem solid var(--bc-panel-bg);
            }}
            .border {{
                padding: {border}rem;
                border-radius: 0.3rem;
                background-color: var(--bc-panel-bg);
            }}
            .color-swatch {{
                padding: 1.4rem;
            }}
            .img-size {{
                height: 1.05rem;
                padding-top: 0.1rem;
                padding-bottom: 0.1rem;
                padding-left: 0.2rem;
                font-size: 0.8rem;
                font-family: system;
                background-color: var(--bc-panel-bg-promoted);
                border-top: 1px solid var(--bc-highlight);
                border-bottom: 1px solid var(--bc-highlight-bottom);
                color: var(--bc-text);
            }}
        </style>
        <div class="preview-bubble bubble-above"></div>
        <div class="border">{content}</div>
    </body>
'''

data_template = 'data:{};base64,{}'

def debug(*msg: str) -> None:
    if sublime.load_settings(SETTINGS_FILE).get('debug', False):
        print('QuickView:', *msg)

def hex2rgba(color: str) -> tuple:
    if len(color) == 5:
        r = int(color[1] * 2, 16)
        g = int(color[2] * 2, 16)
        b = int(color[3] * 2, 16)
        a = int(color[4] * 2, 16) / 255
    elif len(color) == 9:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        a = int(color[7:9], 16) / 255
    else:
        raise ValueError('invalid color ' + color)
    return r, g, b, a

def hex2hsl(color: str) -> tuple:
    r = int(color[1:3], 16) / 255
    g = int(color[3:5], 16) / 255
    b = int(color[5:7], 16) / 255
    maxval = max(r, g, b)
    minval = min(r, g, b)
    c = maxval - minval
    l = (maxval + minval) / 2
    if c == 0:
        h = s = 0
    else:
        if maxval == r:
            h = (g - b) / c % 6
        elif maxval == g:
            h = (b - r) / c + 2
        elif maxval == b:
            h = (r - g) / c + 4
        h = 60 * h
        s = c / (2 - maxval - minval) if l > 0.5 else c / (maxval + minval)
    return h, s, l

@lru_cache(maxsize=128)
def base64png(r1: int, g1: int, b1: int, r2: int, g2: int, b2: int) -> str:
    pixels = list()
    row_type1 = list()
    row_type2 = list()
    for blocks in range(4):
        row_type1.extend([r2, g2, b2] * 5)
        row_type1.extend([r1, g1, b1] * 5)
    for blocks in range(4):
        row_type2.extend([r1, g1, b1] * 5)
        row_type2.extend([r2, g2, b2] * 5)
    row_type1 = tuple(row_type1)
    row_type2 = tuple(row_type2)
    for blocks in range(4):
        for row in range(5):
            pixels.append(row_type1)
        for row in range(5):
            pixels.append(row_type2)
    data = io.BytesIO()
    png.Writer(width=40, height=40, greyscale=False).write(data, pixels)
    data.seek(0)
    return b64encode(data.getvalue()).decode('ascii')

def popup_location(view: sublime.View, region: sublime.Region, popup_width: int) -> int:
    ax, ay = view.text_to_layout(region.a)
    bx, _ = view.text_to_layout(region.b)
    vx = view.viewport_position()[0] + view.viewport_extent()[0] - popup_width  # maximum x-pos so that the popup is still contained within the window
    lx = ax - popup_width / 2  # minimum x-pos so that the popup still points at the link region
    x = (ax + bx - popup_width) / 2
    horizontal_correction = 0
    if x > vx:  # restrict popup position to active viewport
        x = vx
        horizontal_correction = -1  # shift 1 character to the left to ensure that the popup doesn't hide the window border
    if x < lx:  # restrict popup position to link
        x = lx
        horizontal_correction = 1  # shift 1 character to the right to ensure that the popup doesn't point to the left side of potential string punctuation
    return view.layout_to_text((x, ay)) + horizontal_correction

def scale_image(width: int, height: int, em_width: float) -> tuple:
    """
    scale image such that:
    - aspect ratio gets preserved
    - resulting image width is at least MIN_POPUP_IMAGE_WIDTH
    - none of resulting image width and height is larger than MAX_POPUP_IMAGE_WIDTH, unless this contradicts with the previous rule
    """
    device_scale_factor = EM_SCALE_FACTOR * em_width
    if width == -1 or height == -1:  # assume 16:9 aspect ratio
        return int(MAX_POPUP_IMAGE_WIDTH * device_scale_factor), int(9/16 * MAX_POPUP_IMAGE_WIDTH * device_scale_factor)
    image_scale_factor = min(MAX_POPUP_IMAGE_WIDTH / max(width, height), 1)
    scale_correction = max(MIN_POPUP_IMAGE_WIDTH / image_scale_factor / width, 1)
    scale_factor = image_scale_factor * device_scale_factor * scale_correction
    return int(scale_factor * width), int(scale_factor * height)

def image_size_label(width: int, height: int) -> str:
    return '{} \u00d7 {} pixels'.format(width, height)

def format_template(view: sublime.View, popup_width: int, content: str) -> str:
    margin = popup_width / 2 - 9 * EM_SCALE_FACTOR * view.em_width()
    popup_border_width = 0.0725 * sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
    return quickview_template.format(margin=margin, border=popup_border_width, content=content)

@lru_cache(maxsize=16)
def request_img(url: str) -> tuple:
    try:
        debug('requesting image from', url)
        with urllib.request.urlopen(url, timeout=2) as response:
            length = response.headers.get('content-length')
            if length is None:
                raise ValueError('missing content-length header')
            length = int(length)
            if length == 0:
                raise ValueError('empty payload')
            max_payload_size = sublime.load_settings(SETTINGS_FILE).get('max_payload_size', 8096)  # @todo Maybe document this setting?
            mime = response.headers.get('content-type').lower()
            if mime not in SUPPORTED_MIME_TYPES:
                raise ValueError('mime type ' + mime + ' is no image or not supported')
            elif length > max_payload_size * 1024:
                raise ValueError('refusing to download files larger than ' + str(max_payload_size) + ' kB')
            data = response.read()
            width, height = image_size(data)
            data_base64 = b64encode(data).decode('ascii')
            return mime, data_base64, width, height
    except timeout:
        debug(timeout, 'timeout for url', url)
        return None, None, None, None
    except Exception as ex:
        debug(ex, 'for url', url)
        return None, None, None, None

def image_size(data) -> tuple:
    width = -1
    height = -1
    if isinstance(data, bytes):
        data = io.BytesIO(data)
    try:
        head = data.read(26)
        size = len(head)
        # JPEG
        if size >= 2 and head.startswith(b'\377\330'):
            data.seek(0)
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf or ftype in [0xc4, 0xc8, 0xcc]:
                data.seek(size, 1)
                byte = data.read(1)
                while ord(byte) == 0xff:
                    byte = data.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', data.read(2))[0] - 2
            data.seek(1, 1)
            height, width = struct.unpack('>HH', data.read(4))
        # PNG
        elif size >= 24 and head.startswith(b'\211PNG\r\n\032\n') and head[12:16] == b'IHDR':
            width, height = struct.unpack('>LL', head[16:24])
        elif size >= 16 and head.startswith(b'\211PNG\r\n\032\n'):
            width, height = struct.unpack('>LL', head[8:16])
        # GIF
        elif size >= 10 and head[:6] in (b'GIF87a', b'GIF89a'):
            width, height = struct.unpack('<HH', head[6:10])
        # BMP
        elif size >= 26 and head.startswith(b'BM'):
            headerSize = struct.unpack('<I', head[14:18])[0]
            if headerSize == 12:
                width, height = struct.unpack('<HH', head[18:22])
            elif headerSize >= 40:
                width, height = struct.unpack('<ii', head[18:26])
                height = abs(height)
            else:
                raise ValueError('Unknown DIB header size: ' + str(headerSize))
    except Exception:
        pass
    return width, height


class ColorHoverListener(sublime_plugin.ViewEventListener):
    SUPPORTED_SYNTAXES = [
        'CSS.sublime-syntax',
        'HTML.sublime-syntax',
        'PHP.sublime-syntax',
        'LESS.sublime-syntax',
        'Sass.sublime-syntax',
        'SCSS.sublime-syntax',
        'Stylus.tmLanguage'
    ]
    active_region = None

    @classmethod
    def is_applicable(cls, settings: sublime.Settings) -> bool:
        active_syntax = settings.get('syntax')
        for syntax in cls.SUPPORTED_SYNTAXES:
            if syntax in active_syntax:
                return True
        return False

    def on_hover(self, point: int, hover_zone: int) -> None:
        if not sublime.load_settings(SETTINGS_FILE).get('color_preview'):
            return
        if hover_zone != sublime.HOVER_TEXT:
            return
        if self.active_region and self.active_region.contains(point):  # prevent flickering on small cursor movements
            return

        if self.view.match_selector(point, 'support.constant.color.w3c-standard-color-name | support.constant.color.w3c-extended-color-keywords'):
            region = self.view.word(point)
            self.rgb_color_swatch(region)
        elif self.view.match_selector(point, 'constant.other.color.rgb-value - punctuation'):
            region = self.view.word(point)
            region.a = region.a - 1
            self.rgb_color_swatch(region)
        elif self.view.match_selector(point, 'constant.other.color.rgb-value punctuation.definition.constant'):
            region = self.view.word(point + 1)
            region.a = region.a - 1
            self.rgb_color_swatch(region)
        elif self.view.match_selector(point, 'constant.other.color.rgba-value - punctuation'):
            region = self.view.word(point)
            region.a = region.a - 1
            r, g, b, a = hex2rgba(self.view.substr(region))
            self.rgba_color_swatch(region, r, g, b, a)
        elif self.view.match_selector(point, 'constant.other.color.rgba-value punctuation.definition.constant'):
            region = self.view.word(point + 1)
            region.a = region.a - 1
            r, g, b, a = hex2rgba(self.view.substr(region))
            self.rgba_color_swatch(region, r, g, b, a)
        elif self.view.match_selector(point, 'support.function.color | meta.property-value meta.function-call meta.group'):
            line = self.view.line(point)
            text = self.view.substr(line)
            # @see https://facelessuser.github.io/coloraide/color/#color-matching
            for m in COLOR_PATTERN.finditer(text):
                if line.a + m.start() <= point <= line.a + m.end():
                    mcolor = Color.match(text, start=m.start())
                    if mcolor is not None:
                        region = sublime.Region(line.a + mcolor.start, line.a + mcolor.end)
                        mcolor.color.convert('srgb', in_place=True)
                        r = int(255 * mcolor.color.red)
                        g = int(255 * mcolor.color.green)
                        b = int(255 * mcolor.color.blue)
                        a = mcolor.color.alpha
                        self.rgba_color_swatch(region, r, g, b, a)
                    return

    def rgb_color_swatch(self, region: sublime.Region) -> None:
        popup_border_width = sublime.load_settings(SETTINGS_FILE).get("popup_border_width")
        popup_width = int((40 + 2 * popup_border_width) * EM_SCALE_FACTOR * self.view.em_width())
        location = popup_location(self.view, region, popup_width)
        color_swatch = '<div class="color-swatch" style="background-color: {}"></div>'.format(self.view.substr(region))
        content = format_template(self.view, popup_width, color_swatch)
        self.active_region = region
        self.view.show_popup(content, flags, location, 1024, 1024, None, self.reset_active_region)

    def rgba_color_swatch(self, region: sublime.Region, r: int, g: int, b: int, a: float) -> None:
        if a == 1.0:
            self.rgb_color_swatch(region)
            return
        _, _, lightness = hex2hsl(self.view.style()['background'])
        if lightness < 0.5:  # https://www.sublimetext.com/docs/minihtml.html#predefined_classes
            light_pixel = 51 * (1 - a)
            dark_pixel = 0
        else:
            light_pixel = 255 * (1 - a)
            dark_pixel = 204 * (1 - a)
        # @todo Maybe calculate pixel background colors based on actual (popup) background lightness instead of color scheme class?
        r1, g1, b1 = int(r * a + light_pixel), int(g * a + light_pixel), int(b * a + light_pixel)
        r2, g2, b2 = int(r * a + dark_pixel), int(g * a + dark_pixel), int(b * a + dark_pixel)
        data_base64 = base64png(r1, g1, b1, r2, g2, b2)
        device_scale_factor = EM_SCALE_FACTOR * self.view.em_width()
        scaled_width = int(40 * device_scale_factor)
        popup_border_width = sublime.load_settings(SETTINGS_FILE).get("popup_border_width")
        popup_width = int((40 + 2 * popup_border_width) * device_scale_factor)
        location = popup_location(self.view, region, popup_width)
        color_swatch = '<img src="data:image/png;base64,{}" width="{}" height="{}" />'.format(data_base64, scaled_width, scaled_width)
        content = format_template(self.view, popup_width, color_swatch)
        self.active_region = region
        self.view.show_popup(content, flags, location, 1024, 1024, None, self.reset_active_region)

    def reset_active_region(self):
        self.active_region = None


class ImageHoverListener(sublime_plugin.EventListener):
    active_region = None

    def on_hover(self, view: sublime.View, point: int, hover_zone: int) -> None:
        settings = sublime.load_settings(SETTINGS_FILE)
        if not settings.get('image_preview'):
            return
        if hover_zone != sublime.HOVER_TEXT:
            return
        if self.active_region and self.active_region.contains(point):
            return
        scope_selector = settings.get('image_scope_selector')
        if not view.match_selector(point, scope_selector):
            return
        region = view.extract_scope(point)
        url = view.substr(region)
        if view.match_selector(region.a, 'punctuation.definition.string.begin'):
            url = url[1:]
        if view.match_selector(region.b - 1, 'punctuation.definition.string.end'):
            url = url[:-1]
        if url.startswith('data:'):
            try:
                _, mime, _, data_base64 = re.split(r'[:;,]', url)
            except ValueError:
                return
            if mime not in SUPPORTED_MIME_TYPES:
                return
            data = b64decode(data_base64)
            width, height = image_size(data)
            self.create_image_popup(view, region, width, height, url)
        elif url.startswith(SUPPORTED_PROTOCOLS):
            if url.endswith(SUPPORTED_FILE_EXTENSIONS) or settings.get('extensionless_image_preview'):
                sublime.set_timeout_async(lambda: self.request_img_create_popup(url, view, region))
        elif url.endswith(SUPPORTED_FILE_EXTENSIONS):
            file_name = view.file_name()
            if not file_name:
                return
            local_path = os.path.abspath(os.path.join(os.path.dirname(file_name), url))
            if not os.path.exists(local_path):
                return
            debug('loading image from', local_path)
            with open(local_path, 'rb') as data:
                width, height = image_size(data)
            src = 'file://' + local_path
            self.create_image_popup(view, region, width, height, src)

    def request_img_create_popup(self, url: str, view: sublime.View, region: sublime.Region) -> None:
        mime, data_base64, width, height = request_img(url)
        if mime:
            src = data_template.format(mime, data_base64)
            self.create_image_popup(view, region, width, height, src)

    def create_image_popup(self, view: sublime.View, region: sublime.Region, width: int, height: int, src: str) -> None:
        em_width = view.em_width()
        scaled_width, scaled_height = scale_image(width, height, em_width)
        popup_border_width = sublime.load_settings(SETTINGS_FILE).get("popup_border_width")
        popup_width = scaled_width + int(2 * popup_border_width * EM_SCALE_FACTOR * em_width)
        label = image_size_label(width, height)
        img_preview = '<img class="img-preview" src="{}" width="{}" height="{}" /><div class="img-size">{}</div>'.format(src, scaled_width, scaled_height, label)
        location = popup_location(view, region, popup_width)
        content = format_template(view, popup_width, img_preview)
        self.active_region = region
        view.show_popup(content, flags, location, 1024, 1024, None, self.reset_active_region)

    def reset_active_region(self):
        self.active_region = None

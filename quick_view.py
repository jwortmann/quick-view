from base64 import b64encode, b64decode
from coloraide import Color
from colorsys import rgb_to_hls
from functools import lru_cache, partial
from socket import timeout
from typing import Optional, Tuple
from .lib import png
import io
import logging
import os
import re
import struct
import sublime
import sublime_plugin
import subprocess
import urllib.parse, urllib.request


SETTINGS_FILE = 'QuickView.sublime-settings'

EM_SCALE_FACTOR = 1/8.4  # this means the following pixel values correspond to a layout with view.em_width() == 8.4
MIN_POPUP_IMAGE_WIDTH = 100
MAX_POPUP_IMAGE_WIDTH = 200

BACKGROUND_WHITE_PIXEL = {'light': 255, 'dark': 51}
BACKGROUND_BLACK_PIXEL = {'light': 204, 'dark': 0}

SCOPE_SELECTOR_CSS_COLORNAME = 'support.constant.color - support.constant.color.w3c.special - support.constant.color.w3c-special-color-keyword'  # default CSS syntax on ST3 and ST4
SCOPE_SELECTOR_CSS_RGB_LITERAL = 'constant.other.color.rgb-value'  # default CSS syntax
SCOPE_SELECTOR_CSS_RGBA_LITERAL = 'constant.other.color.rgba-value'  # default CSS syntax
SCOPE_SELECTOR_CSS_FUNCTION = 'meta.property-value.css meta.function-call | meta.color.sublime-color-scheme meta.function-call'  # default CSS syntax & PackageDev .sublime-color-scheme syntax
SCOPE_SELECTOR_CSS_CUSTOM_PROPERTY_DEFINITION = 'meta.property-name entity.other.custom-property.css | meta.property-name support.type.custom-property.css'  # default CSS syntax on ST3 and ST4
SCOPE_SELECTOR_CSS_CUSTOM_PROPERTY_REFERENCE = 'meta.property-value variable.other.custom-property.css | meta.property-value support.type.custom-property.css'  # default CSS syntax on ST3 and ST4
SCOPE_SELECTOR_SASS_VARIABLE_DEFINITION = 'variable.declaration.sass'  # Sass package Sass & SCSS syntax
SCOPE_SELECTOR_SASS_VARIABLE_REFERENCE = 'meta.property-value variable.other.sass'  # Sass package Sass & SCSS syntax
SCOPE_SELECTOR_LESS_VARIABLE_DEFINITION = 'variable.declaration.less'  # LESS package
SCOPE_SELECTOR_LESS_VARIABLE_REFERENCE = 'meta.property-value variable.other.less'  # LESS package
SCOPE_SELECTOR_SUBLIME_COLOR_SCHEME_VARIABLE_REFERENCE = 'meta.color.sublime-color-scheme meta.function-call.var variable.other'  # PackageDev .sublime-color-scheme syntax

COLOR_START_PATTERN = re.compile(r'(?i)(?:\b(?<![-#&$])(?:color|hsla?|lch|lab|hwb|rgba?)\(|\b(?<![-#&$])[\w]{3,}(?![(-])\b|(?<![&])#)')
COLOR_FUNCTION_PATTERN = re.compile(r'(?i)(?:\b(?<![-#&$])(?:color|hsla?|lch|lab|hwb|rgba?)\([^)]+\))')
IMAGE_URI_PATTERN = re.compile(r'\bdata:image/(?:png|jpeg|gif|png|svg\+xml|webp|avif)(;base64)?,[A-Za-z0-9+/=]+|\bhttps?://[A-Za-z0-9\-\._~:/?#\[\]@!$&\'()*+,;%=]+\b|(?:[A-Za-z]:)?[^\s:*?"<>|]+\.(?:png|jpg|jpeg|gif|bmp|svg|webp|avif)\b')

DATA_URI_TEMPLATE = 'data:{};base64,{}'

POPUP_TEMPLATE = '''
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
                background-color: {background};
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
                border-radius: {border_radius}rem;
                background-color: var(--bc-panel-bg);
            }}
            .color-swatch {{
                padding: 1.4rem;
            }}
            .img-label {{
                margin-top: {label_top_margin}px;
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
            .icon {{
                color: var(--bc-text);
                text-decoration: none;
            }}
        </style>
        {bubble}
        <div class="border">{content}</div>
    </body>
'''


class ImageFormat:
    UNSUPPORTED = 0
    PNG = 1
    JPEG = 2
    GIF = 3
    BMP = 4
    SVG = 5
    WEBP = 6
    AVIF = 7


class MimeType:
    PNG = 'image/png'
    JPEG = 'image/jpeg'
    GIF = 'image/gif'
    BMP = 'image/bmp'
    SVG = 'image/svg+xml'
    WEBP = 'image/webp'
    AVIF = 'image/avif'


NATIVE_IMAGE_FORMATS = [ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.GIF, ImageFormat.BMP]
CONVERTABLE_IMAGE_FORMATS = [ImageFormat.SVG, ImageFormat.WEBP, ImageFormat.AVIF]
IGNORED_FILE_EXTENSIONS = ('.html', '.css', '.js', '.json', '.md', '.xml', '.mp3', '.ogv', '.mp4', '.mpeg', '.webm', '.zip', '.tgz')

SUPPORTED_MIME_TYPES = [
    MimeType.PNG,
    MimeType.JPEG,
    MimeType.GIF,
    MimeType.BMP,
    MimeType.SVG,
    MimeType.WEBP,
    MimeType.AVIF
]

IMAGE_FORMAT_NAMES = {
    ImageFormat.PNG: 'PNG',
    ImageFormat.JPEG: 'JPEG',
    ImageFormat.GIF: 'GIF',
    ImageFormat.BMP: 'BMP',
    ImageFormat.SVG: 'SVG',
    ImageFormat.WEBP: 'WebP',
    ImageFormat.AVIF: 'AVIF'
}

FILE_EXTENSION_FORMAT_MAP = {
    '.png': ImageFormat.PNG,
    '.jpg': ImageFormat.JPEG,
    '.jpeg': ImageFormat.JPEG,
    '.gif': ImageFormat.GIF,
    '.bmp': ImageFormat.BMP,
    '.svg': ImageFormat.SVG,
    '.webp': ImageFormat.WEBP,
    '.avif': ImageFormat.AVIF
}

MIME_TYPE_FORMAT_MAP = {
    MimeType.PNG: ImageFormat.PNG,
    MimeType.JPEG: ImageFormat.JPEG,
    MimeType.GIF: ImageFormat.GIF,
    MimeType.BMP: ImageFormat.BMP,
    MimeType.SVG: ImageFormat.SVG,
    MimeType.WEBP: ImageFormat.WEBP,
    MimeType.AVIF: ImageFormat.AVIF
}

CONVERTER_SETTING = {
    ImageFormat.SVG: 'svg_converter',
    ImageFormat.WEBP: 'webp_converter',
    ImageFormat.AVIF: 'avif_converter'
}


def format_from_uri(uri: str) -> int:
    """
    Returns the image format for a given URI string based on its file extension
    """
    _, file_extension = os.path.splitext(uri.lower())
    return FILE_EXTENSION_FORMAT_MAP.get(file_extension, ImageFormat.UNSUPPORTED)


def hex2rgba(color: str) -> Tuple[int, int, int, float]:
    """
    Convert hex RGB or RGBA color string into R, G, B, A tuple with integer
    values 0..255 for R, G, B and floating point value [0, 1] for A
    """
    if len(color) == 4:  # 3-digit RGB
        r = int(color[1] * 2, 16)
        g = int(color[2] * 2, 16)
        b = int(color[3] * 2, 16)
        a = 1.0
    if len(color) == 5:  # 4-digit RGBA
        r = int(color[1] * 2, 16)
        g = int(color[2] * 2, 16)
        b = int(color[3] * 2, 16)
        a = int(color[4] * 2, 16) / 255
    elif len(color) == 7:  # 6-digit RGB
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        a = 1.0
    elif len(color) == 9:  # 8-digit RGBA
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        a = int(color[7:9], 16) / 255
    else:
        raise ValueError('invalid color ' + color)
    return r, g, b, a


def match_color(string: str, start: int = 0, fullmatch: bool = False) -> Optional[Tuple[int, int, int, float]]:
    # https://facelessuser.github.io/coloraide/color/#color-matching
    mcolor = Color.match(string, start=start, fullmatch=fullmatch)
    if mcolor is not None:
        mcolor.color.convert('srgb', in_place=True)
        r = int(255 * mcolor.color['red'])
        g = int(255 * mcolor.color['green'])
        b = int(255 * mcolor.color['blue'])
        a = mcolor.color['alpha']
        return r, g, b, a
    else:
        return None


@lru_cache(maxsize=128)
def checkerboard_png(r1: int, g1: int, b1: int, r2: int, g2: int, b2: int) -> str:
    """
    Generate a base64 encoded PNG image with sidelength 40px of a checkerboard pattern with
    color rgb(r1, g1, b1) for the light pixels and color rgb(r2, g2, b2) for the dark pixels.
    The result for given input values is cached to avoid unnecessary calculations for the
    same input values.
    """
    pixels = list()
    row_type1 = list()
    row_type2 = list()
    for _ in range(4):
        row_type1.extend([r2, g2, b2] * 5)
        row_type1.extend([r1, g1, b1] * 5)
    for _ in range(4):
        row_type2.extend([r1, g1, b1] * 5)
        row_type2.extend([r2, g2, b2] * 5)
    row_type1 = tuple(row_type1)
    row_type2 = tuple(row_type2)
    for _ in range(4):
        for _ in range(5):
            pixels.append(row_type1)
        for _ in range(5):
            pixels.append(row_type2)
    data = io.BytesIO()
    png.Writer(width=40, height=40, greyscale=False).write(data, pixels)  # pyright: ignore[reportGeneralTypeIssues]
    data.seek(0)
    return b64encode(data.getvalue()).decode('ascii')


def scale_image(width: int, height: int, device_scale_factor: float) -> Tuple[int, int]:
    """
    Scale image such that:
    - aspect ratio gets preserved
    - resulting image width is at least MIN_POPUP_IMAGE_WIDTH
    - none of resulting image width and height is larger than MAX_POPUP_IMAGE_WIDTH, unless this contradicts with the
      previous rule
    """
    if width == -1 or height == -1:  # assume 1:1 aspect ratio
        scaled_width = int(MIN_POPUP_IMAGE_WIDTH * device_scale_factor)
        return scaled_width, scaled_width
    image_scale_factor = min(MAX_POPUP_IMAGE_WIDTH / max(width, height), 1)
    scale_correction = max(MIN_POPUP_IMAGE_WIDTH / image_scale_factor / width, 1)
    scale_factor = image_scale_factor * device_scale_factor * scale_correction
    return int(scale_factor * width), int(scale_factor * height)


def image_size_label(width: int, height: int) -> str:
    return '{} \u00d7 {} pixels'.format(width, height) if width != -1 else 'unknown size'


@lru_cache(maxsize=16)
def request_img(url: str) -> Tuple[Optional[str], Optional[bytes]]:
    try:
        logging.debug('requesting image from %s', url)
        with urllib.request.urlopen(url, timeout=2) as response:
            length = response.headers.get('content-length')
            if length is None:
                raise ValueError('missing content-length header')
            length = int(length)
            if length == 0:
                raise ValueError('empty payload')
            mime = response.headers.get('content-type').lower()
            if mime not in SUPPORTED_MIME_TYPES:
                raise ValueError('mime type ' + mime + ' is not supported')
            max_payload_size = sublime.load_settings(SETTINGS_FILE).get('max_payload_size', 8096)
            if length > max_payload_size * 1024:
                raise ValueError('refusing to download files larger than ' + str(max_payload_size) + 'kB')
            data = response.read()
            return mime, data
    except timeout:
        logging.debug('timeout for url %s', url)
        return None, None
    except Exception as ex:
        logging.debug(ex)
        return None, None


@lru_cache(maxsize=16)
def convert_bytes2png(data: bytes, input_format: int, converter: str) -> bytes:
    if sublime.platform() == 'windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        startupinfo = None
    if converter == 'inkscape' and input_format == ImageFormat.SVG:
        logging.debug('using Inkscape to convert SVG image')
        p = subprocess.Popen(
            ['inkscape', '--pipe', '--export-type=png'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            startupinfo=startupinfo)
    elif converter == 'dwebp' and input_format == ImageFormat.WEBP:
        logging.debug('using dwebp to convert WebP image')
        p = subprocess.Popen(
            ['dwebp', '-o', '-', '--', '-'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            startupinfo=startupinfo)
    elif converter == 'magick' and input_format in [ImageFormat.SVG, ImageFormat.WEBP, ImageFormat.AVIF]:
        logging.debug('using ImageMagick to convert %s image', IMAGE_FORMAT_NAMES[input_format])
        fmt = {ImageFormat.SVG: 'svg:-', ImageFormat.WEBP: 'webp:-', ImageFormat.AVIF: 'avif:-'}[input_format]
        if sublime.load_settings(SETTINGS_FILE).get('image_background_pattern', True):
            # use checkerboard background pattern for images with transparency
            p = subprocess.Popen(
                ['magick', 'composite', '-compose', 'dst-over', '-tile', 'pattern:checkerboard', '-background', 'transparent', fmt, 'png:-'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                startupinfo=startupinfo)
        else:
            p = subprocess.Popen(
                ['magick', '-background', 'transparent', fmt, 'png:-'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                startupinfo=startupinfo)
    else:
        raise ValueError('unknown converter {} or incompatible image format'.format(converter))
    png, _ = p.communicate(data)
    p.stdin.close()  # type: ignore
    return png


def convert_file2png(path: str, input_format: int, converter: str) -> bytes:
    if sublime.platform() == 'windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        startupinfo = None
    if converter == 'inkscape' and input_format == ImageFormat.SVG:
        logging.debug('using Inkscape to convert SVG image')
        png = subprocess.check_output(
            ['inkscape', '--export-type=png', '--export-filename=-', path],
            startupinfo=startupinfo)
    elif converter == 'dwebp' and input_format == ImageFormat.WEBP:
        logging.debug('using dwebp to convert WebP image')
        png = subprocess.check_output(['dwebp', '-o', '-', '--', path], startupinfo=startupinfo)
    elif converter == 'magick' and input_format in [ImageFormat.SVG, ImageFormat.WEBP, ImageFormat.AVIF]:
        logging.debug('using ImageMagick to convert %s image', IMAGE_FORMAT_NAMES[input_format])
        if sublime.load_settings(SETTINGS_FILE).get('image_background_pattern', True):
            png = subprocess.check_output(
                ['magick', 'composite', '-compose', 'dst-over', '-tile', 'pattern:checkerboard', '-background', 'transparent', path, 'png:-'],
                startupinfo=startupinfo)
        else:
            png = subprocess.check_output(
                ['magick', '-background', 'transparent', path, 'png:-'],
                startupinfo=startupinfo)
    else:
        raise ValueError('unknown converter {} or incompatible image format'.format(converter))
    return png


def image_size(data) -> Tuple[int, int]:
    """
    Extract image width and height from the file header
    """
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
        elif size >= 10 and head.startswith((b'GIF87a', b'GIF89a')):
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
                raise ValueError('unknown DIB header size: ' + str(headerSize))
    except Exception as ex:
        logging.debug(ex)
    return width, height


# https://en.wikipedia.org/wiki/Data_URI_scheme#Syntax
def parse_data_uri(uri: str) -> Tuple[str, bytes]:
    if not uri.startswith('data:') or ',' not in uri:
        raise ValueError('invalid data uri')
    media_type, _, raw_data = uri[5:].partition(',')
    data = b64decode(raw_data) if media_type.endswith(';base64') else urllib.parse.unquote_to_bytes(raw_data)
    mime = media_type.split(';')[0] if media_type else 'text/plain'
    return mime, data


class QuickViewHoverListener(sublime_plugin.EventListener):

    def on_hover(self, view: sublime.View, point: int, hover_zone: int) -> None:
        if hover_zone != sublime.HOVER_TEXT:
            return
        view.run_command('quick_view', {'point': point})


class QuickViewCommand(sublime_plugin.TextCommand):
    _active_region = None  # type: Optional[sublime.Region]

    def run(self, edit: sublime.Edit, point: Optional[int] = None) -> None:
        manual = point is None
        if manual:
            if self._active_region:
                self.view.hide_popup()
                return
            try:
                region = self.view.sel()[0]
                point = region.begin()
            except IndexError:
                logging.error('no selections in the active view')
                return
            empty_selection = region.empty()
            if empty_selection:
                region = self.view.line(point)
            elif len(self.view.lines(region)) > 1:
                self.view.window().status_message('QuickView not possible for selections that span multiple lines')  # pyright: ignore[reportOptionalMemberAccess]
                return
        elif self._active_region and self._active_region.contains(point):  # prevent flickering on small mouse movements
            return
        settings = sublime.load_settings(SETTINGS_FILE)
        if manual or settings.get('image_preview'):
            if self.view.match_selector(point, settings.get('image_scope_selector')):
                region = self.view.extract_scope(point)
                self.image_preview(region, manual)
                return
        if manual or settings.get('color_preview'):
            if self.view.match_selector(point, SCOPE_SELECTOR_CSS_COLORNAME):
                region = self.view.word(point)
                self.color_preview_rgb(region)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_CSS_RGB_LITERAL):
                region = self.view.extract_scope(point)
                # fix for punctuation scope from default CSS package
                if region.size() == 1:
                    region = self.view.extract_scope(point + 1)
                # fix for unconventional scopes from SCSS package
                elif region.a > 0 and region.size() in [3, 6] and self.view.substr(region.a - 1) == '#':
                    region.a -= 1
                self.color_preview_rgb(region)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_CSS_RGBA_LITERAL):
                region = self.view.extract_scope(point)
                # fix for punctuation scope from default CSS package
                if region.size() == 1:
                    region = self.view.extract_scope(point + 1)
                color_tuple = hex2rgba(self.view.substr(region))
                self.color_preview_rgba(region, color_tuple)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_CSS_CUSTOM_PROPERTY_REFERENCE):
                region = self.view.extract_scope(point)
                self.color_preview_css_variable(region, SCOPE_SELECTOR_CSS_CUSTOM_PROPERTY_DEFINITION, manual)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_SASS_VARIABLE_DEFINITION):
                region = self.view.extract_scope(point)
                self.color_preview_css_variable(region, SCOPE_SELECTOR_SASS_VARIABLE_DEFINITION, manual)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_LESS_VARIABLE_DEFINITION):
                region = self.view.extract_scope(point)
                self.color_preview_css_variable(region, SCOPE_SELECTOR_LESS_VARIABLE_DEFINITION, manual)
                return
            elif self.view.match_selector(point, SCOPE_SELECTOR_SUBLIME_COLOR_SCHEME_VARIABLE_REFERENCE):
                region = self.view.extract_scope(point)
                self.color_preview_color_scheme_variable(region, manual)
                return
            # scope for CSS functions should be checked last, because the scope also matches for custom properties
            elif self.view.match_selector(point, SCOPE_SELECTOR_CSS_FUNCTION):
                regions = self.view.find_by_selector(SCOPE_SELECTOR_CSS_FUNCTION)
                for region in regions:
                    if region.contains(point):
                        logging.debug(self.view.substr(region))
                        if self.view.match_selector(region.a, 'support.function.color'):
                            color_tuple = match_color(self.view.substr(region), fullmatch=True)
                            if color_tuple is not None:
                                self.color_preview_rgba(region, color_tuple)
                                return
                        # elif view.match_selector(region.a, 'support.function.gradient'):
                        #     if view.substr(region).startswith('linear-gradient'):
                        #         pass
                        break
                return
        if manual:
            text = self.view.substr(region)  # pyright: ignore[reportUnboundVariable]
            offset = region.begin()  # pyright: ignore[reportUnboundVariable]
            for m in IMAGE_URI_PATTERN.finditer(text):
                # if selection is empty ensure cursor position is within the found region
                if not empty_selection or m.start() <= point - offset <= m.end():  # pyright: ignore[reportUnboundVariable]
                    link_region = sublime.Region(offset + m.start(), offset + m.end())
                    logging.debug('potential image URI found: %s', self.view.substr(link_region))
                    self.image_preview(region, True)  # pyright: ignore[reportUnboundVariable]
                    return
            for m in COLOR_START_PATTERN.finditer(text):
                if not empty_selection or m.start() <= point - offset:  # pyright: ignore[reportUnboundVariable]
                    mcolor = Color.match(text, start=m.start())
                    if mcolor is not None and (not empty_selection or point - offset <= mcolor.end):  # pyright: ignore[reportUnboundVariable]
                        color_region = sublime.Region(offset + mcolor.start, offset + mcolor.end)
                        mcolor.color.convert('srgb', in_place=True)
                        r = int(255 * mcolor.color['red'])
                        g = int(255 * mcolor.color['green'])
                        b = int(255 * mcolor.color['blue'])
                        a = mcolor.color['alpha']
                        self.color_preview_rgba(color_region, (r, g, b, a))
                        return

    def expand_local_path(self, path: str) -> str:
        directory_path, filename = os.path.split(path)  # don't expand variables within the filename
        variables = self.view.window().extract_variables()  # pyright: ignore[reportOptionalMemberAccess]
        for alias, replacement in sublime.load_settings(SETTINGS_FILE).get('path_aliases').items():
            if directory_path.startswith(alias):
                replacement = sublime.expand_variables(replacement, variables)
                directory_path = directory_path.replace(alias, replacement, 1)
                break
        full_path = os.path.join(directory_path, filename)  # join back together
        if os.path.isabs(full_path):
            return full_path
        else:
            file_name = self.view.file_name()
            return os.path.abspath(os.path.join(os.path.dirname(file_name), full_path)) if file_name else ''

    def popup_content(self, content: str, popup_width: int) -> str:
        popup_style = sublime.load_settings(SETTINGS_FILE).get('popup_style')
        bubble = '<div class="preview-bubble bubble-above"></div>' if 'pointer' in popup_style else ''
        popup_border_radius = 0.3 if 'rounded' in popup_style else 0
        margin = popup_width / 2 - 9 * EM_SCALE_FACTOR * self.view.em_width()
        popup_border_width = 0.0725 * sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
        label_top_margin = 1 if int(sublime.version()) >= 4000 else 0
        popup_shadows = sublime.load_settings('Preferences.sublime-settings').get('popup_shadows', False)
        background = 'color(var(--background) lightness(- 1.2%))' if popup_shadows else 'var(--background)'
        return POPUP_TEMPLATE.format(
            background=background,
            margin=margin,
            border=popup_border_width,
            border_radius=popup_border_radius,
            label_top_margin=label_top_margin,
            bubble=bubble,
            content=content)

    def popup_location(self, region: sublime.Region, popup_width: int) -> int:
        ax, ay = self.view.text_to_layout(region.a)
        bx, _ = self.view.text_to_layout(region.b)
        # minimum x-pos so that the popup is still contained within the view
        view_ax = self.view.viewport_position()[0]
        # maximum x-pos so that the popup is still contained within the view
        view_bx = view_ax + self.view.viewport_extent()[0] - popup_width
        # minimum x-pos so that the popup still points at the link region
        link_ax = ax - popup_width / 2
        # maximum x-pos so that the popup still points at the link region
        link_bx = bx - popup_width / 2
        x = (ax + bx - popup_width) / 2
        horizontal_correction = 0
        if x < view_ax:  # restrict popup position to active viewport (left side)
            x = view_ax
            horizontal_correction = 1  # add padding between popup and left window border
            if x > link_bx:  # restrict popup position to link
                x = link_bx
                horizontal_correction = -1  # add padding between popup and right link boundary
        if x > view_bx:  # restrict popup position to active viewport (right side)
            x = view_bx
            horizontal_correction = -1  # add padding between popup and right window border
            if x < link_ax:  # restrict popup position to link
                x = link_ax
                horizontal_correction = 1  # add padding between popup and left link boundary
        return self.view.layout_to_text((x, ay)) + horizontal_correction

    def show_popup(self, region: sublime.Region, content: str) -> None:
        popup_border_width = sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
        popup_width = int((40 + 2 * popup_border_width) * EM_SCALE_FACTOR * self.view.em_width())
        location = self.popup_location(region, popup_width)
        content = self.popup_content(content, popup_width)
        self.set_active_region(region)
        self.view.show_popup(
            content,
            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=location,
            max_width=1024,
            max_height=1024,
            on_navigate=None,
            on_hide=self.reset_active_region)

    def show_image_popup(self, region: sublime.Region, width: int, height: int, src: str, title: str) -> None:
        sublime_version = int(sublime.version())
        def on_navigate(href: str) -> None:
            sublime.active_window().open_file(href[len('file://'):])
        device_scale_factor = EM_SCALE_FACTOR * self.view.em_width()
        scaled_width, scaled_height = scale_image(width, height, device_scale_factor)
        settings = sublime.load_settings(SETTINGS_FILE)
        popup_border_width = settings.get('popup_border_width')
        popup_width = scaled_width + int(2 * popup_border_width * device_scale_factor)
        label = image_size_label(width, height)
        if 'open_image_button' in settings.get('popup_style'):
            if src.startswith('file://') or sublime_version >= 4096 and src.startswith('data:'):
                href = sublime.command_url('quick_view_open_image', {'href': src, 'title': title}) if sublime_version >= 4096 else src
                label += '<span>&nbsp;&nbsp;&nbsp;</span><a class="icon" href="{}" title="Open Image in new Tab">❐</a>'.format(href)
        content = '<img src="{}" width="{}" height="{}" /><div class="img-label">{}</div>'.format(src, scaled_width, scaled_height, label)
        location = self.popup_location(region, popup_width)
        content = self.popup_content(content, popup_width)
        self.set_active_region(region)
        self.view.show_popup(
            content,
            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=location,
            max_width=1024,
            max_height=1024,
            on_navigate=on_navigate if sublime_version < 4096 else None,
            on_hide=self.reset_active_region)

    def image_preview(self, region: sublime.Region, show_errors: bool = False) -> None:
        uri = self.view.substr(region)
        # remove possible string quotes
        if self.view.match_selector(region.begin(), 'punctuation.definition.string.begin | punctuation.definition.link.begin'):
            uri = uri[1:]
        if self.view.match_selector(region.end() - 1, 'punctuation.definition.string.end | punctuation.definition.link.end'):
            uri = uri[:-1]
        if uri.startswith('data:'):
            sublime.set_timeout_async(partial(self.data_uri_image_preview, region, uri, show_errors))
        else:
            image_format = format_from_uri(uri)
            settings = sublime.load_settings(SETTINGS_FILE)
            if image_format in CONVERTABLE_IMAGE_FORMATS:
                setting = CONVERTER_SETTING[image_format]
                converters = {
                    ImageFormat.SVG: ('inkscape', 'magick'),
                    ImageFormat.WEBP: ('dwebp', 'magick'),
                    ImageFormat.AVIF: ('magick')
                }[image_format]
                if settings.get(setting) not in converters:
                    if show_errors:
                        self.view.window().status_message('No valid {} converter set in the package settings'.format(IMAGE_FORMAT_NAMES[image_format]))  # pyright: ignore[reportOptionalMemberAccess]
                    return
            if uri.lower().startswith(('http:', 'https:', 'ftp:')):
                if image_format in NATIVE_IMAGE_FORMATS + CONVERTABLE_IMAGE_FORMATS or \
                    (settings.get('extensionless_image_preview') and not uri.lower().endswith(IGNORED_FILE_EXTENSIONS)):
                    sublime.set_timeout_async(partial(self.internet_url_image_preview, region, uri, show_errors))
            elif uri.startswith('file://'):  # local absolute path
                if image_format in NATIVE_IMAGE_FORMATS + CONVERTABLE_IMAGE_FORMATS:
                    sublime.set_timeout_async(partial(self.local_path_image_preview, region, uri[len('file://'):], show_errors))
            else:  # local relative path
                if image_format in NATIVE_IMAGE_FORMATS + CONVERTABLE_IMAGE_FORMATS:
                    sublime.set_timeout_async(partial(self.local_path_image_preview, region, self.expand_local_path(uri), show_errors))

    def data_uri_image_preview(self, region: sublime.Region, data_uri: str, show_errors: bool = False) -> None:
        try:
            mime, data = parse_data_uri(data_uri)
        except Exception as ex:
            logging.debug(ex)
            if show_errors:
                self.view.window().status_message('Parsing error for data URI')  # pyright: ignore[reportOptionalMemberAccess]
            return
        if mime in (MimeType.PNG, MimeType.JPEG, MimeType.GIF, MimeType.BMP):
            pass
        elif mime == MimeType.SVG:
            converter = sublime.load_settings(SETTINGS_FILE).get('svg_converter')
            try:
                data = convert_bytes2png(data, ImageFormat.SVG, converter)
            except Exception as ex:
                logging.debug(ex)
                if show_errors:
                    self.view.window().status_message('Conversion error for SVG data URI')  # pyright: ignore[reportOptionalMemberAccess]
                return
            data_base64 = b64encode(data).decode('ascii')
            data_uri = DATA_URI_TEMPLATE.format(MimeType.PNG, data_base64)
        elif mime == MimeType.WEBP:
            converter = sublime.load_settings(SETTINGS_FILE).get('webp_converter')
            try:
                data = convert_bytes2png(data, ImageFormat.WEBP, converter)
            except Exception as ex:
                logging.debug(ex)
                if show_errors:
                    self.view.window().status_message('Conversion error for WebP data URI')  # pyright: ignore[reportOptionalMemberAccess]
                return
            data_base64 = b64encode(data).decode('ascii')
            data_uri = DATA_URI_TEMPLATE.format(MimeType.PNG, data_base64)
        elif mime == MimeType.AVIF:
            converter = sublime.load_settings(SETTINGS_FILE).get('avif_converter')
            try:
                data = convert_bytes2png(data, ImageFormat.AVIF, converter)
            except Exception as ex:
                logging.debug(ex)
                if show_errors:
                    self.view.window().status_message('Conversion error for AVIF data URI')  # pyright: ignore[reportOptionalMemberAccess]
                return
            data_base64 = b64encode(data).decode('ascii')
            data_uri = DATA_URI_TEMPLATE.format(MimeType.PNG, data_base64)
        else:
            if show_errors:
                self.view.window().status_message('Mime type {} for data URI not supported'.format(mime))  # pyright: ignore[reportOptionalMemberAccess]
            return
        width, height = image_size(data)
        title = 'data URI image ({})'.format(IMAGE_FORMAT_NAMES[MIME_TYPE_FORMAT_MAP[mime]])
        self.show_image_popup(region, width, height, data_uri, title)

    def internet_url_image_preview(self, region: sublime.Region, url: str, show_errors: bool = False) -> None:
        logging.debug('potential image URL: %s', url)
        mime, data = request_img(url)
        if not mime or not data:
            if show_errors:
                self.view.window().status_message('QuickView not possible for URL {}'.format(url))  # pyright: ignore[reportOptionalMemberAccess]
            return
        image_format = MIME_TYPE_FORMAT_MAP.get(mime, ImageFormat.UNSUPPORTED)
        if image_format in CONVERTABLE_IMAGE_FORMATS:
            converter = sublime.load_settings(SETTINGS_FILE).get(CONVERTER_SETTING[image_format])
            mime = MimeType.PNG
            try:
                data = convert_bytes2png(data, image_format, converter)
            except Exception as ex:
                logging.debug(ex)
                if show_errors:
                    self.view.window().status_message('Image conversion error for url {}'.format(url))  # pyright: ignore[reportOptionalMemberAccess]
                return
        width, height = image_size(data)
        parsed = urllib.parse.urlparse(url)
        title = os.path.basename(parsed.path)
        data_base64 = b64encode(data).decode('ascii')
        data_uri = DATA_URI_TEMPLATE.format(mime, data_base64)
        self.show_image_popup(region, width, height, data_uri, title)

    def local_path_image_preview(self, region: sublime.Region, path: str, show_errors: bool = False) -> None:
        if not os.path.isfile(path):
            if show_errors:
                self.view.window().status_message('File {} was not found'.format(path))  # pyright: ignore[reportOptionalMemberAccess]
            return
        logging.debug('loading local image from %s', path)
        image_format = format_from_uri(path)
        if image_format in CONVERTABLE_IMAGE_FORMATS:
            converter = sublime.load_settings(SETTINGS_FILE).get({ImageFormat.SVG: 'svg_converter', ImageFormat.WEBP: 'webp_converter', ImageFormat.AVIF: 'avif_converter'}[image_format])
            try:
                data = convert_file2png(path, image_format, converter)
            except Exception as ex:
                logging.debug(ex)
                if show_errors:
                    self.view.window().status_message('Image conversion error for file {}'.format(path))  # pyright: ignore[reportOptionalMemberAccess]
                return
            width, height = image_size(data)
            data_base64 = b64encode(data).decode('ascii')
            src = DATA_URI_TEMPLATE.format(MimeType.PNG, data_base64)
        else:
            with open(path, 'rb') as data:
                width, height = image_size(data)
            src = 'file://' + path
        title = os.path.basename(path)
        self.show_image_popup(region, width, height, src, title)

    def color_preview_rgb(self, region: sublime.Region) -> None:
        content = '<div class="color-swatch" style="background-color: {}"></div>'.format(self.view.substr(region))
        self.show_popup(region, content)

    def color_preview_rgba(self, region: sublime.Region, color_tuple: Tuple[int, int, int, float]) -> None:
        r, g, b, a = color_tuple
        # ensure RGB values are in range 0..255
        if any([val not in range(0, 256) for val in (r, g, b)]):
            logging.debug('invalid RGB color rgb(%i, %i, %i)', r, g, b)
            return
        # ensure alpha value is in range [0, 1]
        if not 0.0 <= a <= 1.0:
            logging.debug('invalid alpha value %f', a)
            return
        if a == 1.0:
            content = '<div class="color-swatch" style="background-color: rgb({}, {}, {})"></div>'.format(r, g, b)
        else:
            r0, g0, b0, _ = hex2rgba(self.view.style()['background'])
            _, lightness, _ = rgb_to_hls(r0/255, g0/255, b0/255)
            color_scheme_type = 'dark' if lightness < 0.5 else 'light'  # https://www.sublimetext.com/docs/minihtml.html#predefined_classes
            bg_white = BACKGROUND_WHITE_PIXEL[color_scheme_type] * (1 - a)
            bg_black = BACKGROUND_BLACK_PIXEL[color_scheme_type] * (1 - a)
            r1, g1, b1 = int(r * a + bg_white), int(g * a + bg_white), int(b * a + bg_white)
            r2, g2, b2 = int(r * a + bg_black), int(g * a + bg_black), int(b * a + bg_black)
            data_base64 = checkerboard_png(r1, g1, b1, r2, g2, b2)
            scaled_width = int(40 * EM_SCALE_FACTOR * self.view.em_width())
            content = '<img src="data:image/png;base64,{}" width="{}" height="{}" />'.format(data_base64, scaled_width, scaled_width)
        self.show_popup(region, content)

    def color_preview_css_variable(self, region: sublime.Region, definition_selector: str, show_errors: bool = False) -> None:
        variable_name = self.view.substr(region)
        definition_regions = [r for r in self.view.find_by_selector(definition_selector) if self.view.substr(r) == variable_name]
        # only proceed if there is exactly 1 definition for the variable/custom property, because this implementation is
        # not aware of CSS rule scopes or possible inheritance resulting from the HTML structure
        if len(definition_regions) == 0:
            if show_errors:
                self.view.window().status_message('No definition found for variable {}'.format(variable_name))  # pyright: ignore[reportOptionalMemberAccess]
            return
        elif len(definition_regions) > 1:
            if show_errors:
                self.view.window().status_message('More than one definition found for variable {}'.format(variable_name))  # pyright: ignore[reportOptionalMemberAccess]
            return
        # extract next token
        a = self.view.find(r'\S', definition_regions[0].b).a
        msg = 'No valid color could be identified for variable {}'.format(variable_name)
        if self.view.substr(a) != ':':
            if show_errors:
                self.view.window().status_message(msg)  # pyright: ignore[reportOptionalMemberAccess]
            return
        a += 1
        b = self.view.find_by_class(definition_regions[0].b, forward=True, classes=sublime.CLASS_LINE_END)
        if a >= b:
            if show_errors:
                self.view.window().status_message(msg)  # pyright: ignore[reportOptionalMemberAccess]
            return
        value_region = sublime.Region(a, b)
        text = re.split('[;}]', self.view.substr(value_region))[0].strip()
        color_tuple = match_color(text, fullmatch=True)
        if color_tuple is None:
            if show_errors:
                self.view.window().status_message(msg)  # pyright: ignore[reportOptionalMemberAccess]
            return
        self.color_preview_rgba(region, color_tuple)

    def color_preview_color_scheme_variable(self, region: sublime.Region, show_errors: bool = False) -> None:
        filename = self.view.file_name()
        variable_name = self.view.substr(region)
        value = None
        if filename:  # search for variable also in overridden files
            data_path = os.path.dirname(sublime.packages_path())
            for resource in sublime.find_resources(os.path.basename(filename)):
                try:
                    is_current_view = os.path.samefile(filename, os.path.join(data_path, resource))
                    # use buffer content for current view, because there can be unsaved changes
                    content = sublime.decode_value(self.view.substr(sublime.Region(0, self.view.size()))) if is_current_view else \
                        sublime.decode_value(sublime.load_resource(resource))
                    value = content['variables'][variable_name]
                except:
                    pass
        else:  # search for variable only in current view
            try:
                content = sublime.decode_value(self.view.substr(sublime.Region(0, self.view.size())))
                value = content['variables'][variable_name]
            except:  # TODO try to resolve variable via scope name like in CSS
                pass
        if isinstance(value, str):
            # TODO also support minihtml color() mod function
            color_tuple = match_color(value, fullmatch=True)
            if color_tuple is None:
                if show_errors:
                    self.view.window().status_message('No valid color could be identified for variable {}'.format(variable_name))  # pyright: ignore[reportOptionalMemberAccess]
                return
            self.color_preview_rgba(region, color_tuple)

    def set_active_region(self, region: sublime.Region) -> None:
        self._active_region = region

    def reset_active_region(self) -> None:
        self._active_region = None


class QuickViewOpenImageCommand(sublime_plugin.WindowCommand):
    def run(self, event: dict, href: str, title: str) -> None:
        assert int(sublime.version()) >= 4096, 'This command only works on ST build 4096 or newer'
        flags = sublime.FORCE_GROUP
        if 'primary' in event['modifier_keys']:
            flags |= sublime.ADD_TO_SELECTION | sublime.SEMI_TRANSIENT
        if href.startswith('file://'):
            path = href[len('file://'):]
            self.window.open_file(path, flags)
        elif href.startswith('data:'):
            contents = '<div style="text-align: center;"><img src="{}" /></div>'.format(href)
            self.window.new_html_sheet(title, contents, flags)

    def want_event(self) -> bool:
        return True

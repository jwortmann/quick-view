import io
import os
import re
import struct
import sublime
import sublime_plugin
import subprocess
import urllib.parse, urllib.request

from base64 import b64encode, b64decode
from coloraide import Color
from functools import lru_cache
from socket import timeout
from .lib import png


SETTINGS_FILE = 'QuickView.sublime-settings'

EM_SCALE_FACTOR = 1/8.4  # this means the following pixel values correspond to a layout with view.em_width() == 8.4
MIN_POPUP_IMAGE_WIDTH = 100
MAX_POPUP_IMAGE_WIDTH = 200

BACKGROUND_WHITE_PIXEL = {'light': 255, 'dark': 51}
BACKGROUND_BLACK_PIXEL = {'light': 204, 'dark': 0}

SUPPORTED_PROTOCOLS = ('http:', 'https:', 'ftp:')

COLOR_START_PATTERN = re.compile(r'(?i)(?:\b(?<![-#&$])(?:color|hsla?|lch|lab|hwb|rgba?)\(|\b(?<![-#&$])[\w]{3,}(?![(-])\b|(?<![&])#)')
COLOR_FUNCTION_PATTERN = re.compile(r'(?i)(?:\b(?<![-#&$])(?:color|hsla?|lch|lab|hwb|rgba?)\([^)]+\))')
IMAGE_LINK_PATTERN = re.compile(r'\bdata:image/(?:png|jpeg|gif|png|svg\+xml|webp|avif)(;base64)?,[A-Za-z0-9+/=]+|\bhttps?://[A-Za-z0-9\-\._~:/?#\[\]@!$&\'()*+,;%=]+\b|(?:[A-Za-z]:)?[^\s:*?"<>|]+\.(?:png|jpg|jpeg|gif|bmp|svg|webp|avif)\b')

POPUP_FLAGS = sublime.HIDE_ON_MOUSE_MOVE_AWAY

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
                border-radius: {border_radius}rem;
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
        {bubble}
        <div class="border">{content}</div>
    </body>
'''

data_uri_template = 'data:{};base64,{}'

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
SUPPORTED_MIME_TYPES = [MimeType.PNG, MimeType.JPEG, MimeType.GIF, MimeType.BMP, MimeType.SVG, MimeType.WEBP, MimeType.AVIF]

SUPPORTED_CONVERTERS = {
    ImageFormat.SVG: ['inkscape', 'magick'],
    ImageFormat.WEBP: ['dwebp', 'magick'],
    ImageFormat.AVIF: ['magick']
}

IMAGE_FORMAT_NAMES = {
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

def format_from_url(url: str) -> int:
    _, file_extension = os.path.splitext(url.lower())
    return FILE_EXTENSION_FORMAT_MAP[file_extension] if file_extension in FILE_EXTENSION_FORMAT_MAP else ImageFormat.UNSUPPORTED

def debug(*msg) -> None:
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
    h = 0
    s = 0
    l = (maxval + minval) / 2
    if c != 0:
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
def checkerboard_png(r1: int, g1: int, b1: int, r2: int, g2: int, b2: int) -> str:
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
    png.Writer(width=40, height=40, greyscale=False).write(data, pixels)
    data.seek(0)
    return b64encode(data.getvalue()).decode('ascii')

def popup_location(view: sublime.View, region: sublime.Region, popup_width: int) -> int:
    """
    calculate popup location such that:
    - the popup points the region
    - the popup is fully contained within the view and doesn't overlap with the window border, unless this contradicts with the previous rule
    - the popup points to the center of the region, unless this contradicts with the previous rule
    """
    ax, ay = view.text_to_layout(region.a)
    bx, _ = view.text_to_layout(region.b)
    view_ax = view.viewport_position()[0]  # minimum x-pos so that the popup is still contained within the view
    view_bx = view_ax + view.viewport_extent()[0] - popup_width  # maximum x-pos so that the popup is still contained within the view
    link_ax = ax - popup_width / 2  # minimum x-pos so that the popup still points at the link region
    link_bx = bx - popup_width / 2  # maximum x-pos so that the popup still points at the link region
    x = (ax + bx - popup_width) / 2
    horizontal_correction = 0
    if x < view_ax:  # restrict popup position to active viewport (left side)
        x = view_ax
        horizontal_correction = 1  # shift 1 character to the right to ensure that the popup doesn't hide the left window border
        if x > link_bx:  # restrict popup position to link
            x = link_bx
            horizontal_correction = -1  # shift 1 character to the left to ensure that the popup doesn't point to the right side of potential string punctuation
    if x > view_bx:  # restrict popup position to active viewport (right side)
        x = view_bx
        horizontal_correction = -1  # shift 1 character to the left to ensure that the popup doesn't hide the right window border
        if x < link_ax:  # restrict popup position to link
            x = link_ax
            horizontal_correction = 1  # shift 1 character to the right to ensure that the popup doesn't point to the left side of potential string punctuation
    return view.layout_to_text((x, ay)) + horizontal_correction

def scale_image(width: int, height: int, device_scale_factor: float) -> tuple:
    """
    scale image such that:
    - aspect ratio gets preserved
    - resulting image width is at least MIN_POPUP_IMAGE_WIDTH
    - none of resulting image width and height is larger than MAX_POPUP_IMAGE_WIDTH, unless this contradicts with the previous rule
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

def format_template(view: sublime.View, popup_width: int, content: str) -> str:
    popup_style = sublime.load_settings(SETTINGS_FILE).get('popup_style')
    bubble = '<div class="preview-bubble bubble-above"></div>' if 'pointer' in popup_style else ''
    popup_border_radius = 0.3 if 'rounded' in popup_style else 0
    margin = popup_width / 2 - 9 * EM_SCALE_FACTOR * view.em_width()  # @todo Does this work on high DPI displays? Should maybe better use rem units instead of px here
    popup_border_width = 0.0725 * sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
    return quickview_template.format(margin=margin, border=popup_border_width, border_radius=popup_border_radius, bubble=bubble, content=content)

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
            mime = response.headers.get('content-type').lower()
            if mime not in SUPPORTED_MIME_TYPES:
                raise ValueError('mime type ' + mime + ' is not supported')
            max_payload_size = sublime.load_settings(SETTINGS_FILE).get('max_payload_size', 8096)  # @todo Maybe document this setting?
            if length > max_payload_size * 1024:
                raise ValueError('refusing to download files larger than ' + str(max_payload_size) + 'kB')
            data = response.read()
            return mime, data
    except timeout:
        debug('timeout for url', url)
        return None, None
    except Exception as ex:
        debug(ex, 'for url', url)
        return None, None

@lru_cache(maxsize=16)
def convert_bytes2png(data: bytes, input_format: int, converter: str) -> bytes:
    if sublime.platform() == 'windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        startupinfo = None
    if converter == 'inkscape' and input_format == ImageFormat.SVG:
        debug('using Inkscape to convert SVG image')
        p = subprocess.Popen(['inkscape', '--pipe', '--export-type=png'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=startupinfo)
    elif converter == 'dwebp' and input_format == ImageFormat.WEBP:
        debug('using dwebp to convert WebP image')
        p = subprocess.Popen(['dwebp', '-o', '-', '--', '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=startupinfo)
    elif converter == 'magick' and input_format in [ImageFormat.SVG, ImageFormat.WEBP, ImageFormat.AVIF]:
        debug('using ImageMagick to convert', IMAGE_FORMAT_NAMES[input_format], 'image')
        fmt = {ImageFormat.SVG: 'svg:-', ImageFormat.WEBP: 'webp:-', ImageFormat.AVIF: 'avif:-'}[input_format]
        if sublime.load_settings(SETTINGS_FILE).get('background_pattern', False):  # @todo Experimental setting: use checkerboard background pattern for images with transparency
            p = subprocess.Popen(['magick', 'composite', '-compose', 'dst-over', '-tile', 'pattern:checkerboard', '-background', 'transparent', fmt, 'png:-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=startupinfo)
        else:
            p = subprocess.Popen(['magick', '-background', 'transparent', fmt, 'png:-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=startupinfo)
    else:
        raise ValueError('unknown converter {} or incompatible image format'.format(converter))
    png, _ = p.communicate(data)
    p.stdin.close()
    return png

def convert_file2png(path: str, input_format: int, converter: str) -> bytes:
    if sublime.platform() == 'windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        startupinfo = None
    if converter == 'inkscape' and input_format == ImageFormat.SVG:
        debug('using Inkscape to convert SVG image')
        png = subprocess.check_output(['inkscape', '--export-type=png', '--export-filename=-', path], startupinfo=startupinfo)
    elif converter == 'dwebp' and input_format == ImageFormat.WEBP:
        debug('using dwebp to convert WebP image')
        png = subprocess.check_output(['dwebp', '-o', '-', '--', path], startupinfo=startupinfo)
    elif converter == 'magick' and input_format in [ImageFormat.SVG, ImageFormat.WEBP, ImageFormat.AVIF]:
        debug('using ImageMagick to convert', IMAGE_FORMAT_NAMES[input_format], 'image')
        if sublime.load_settings(SETTINGS_FILE).get('background_pattern', False):
            png = subprocess.check_output(['magick', 'composite', '-compose', 'dst-over', '-tile', 'pattern:checkerboard', '-background', 'transparent', path, 'png:-'], startupinfo=startupinfo)
        else:
            png = subprocess.check_output(['magick', '-background', 'transparent', path, 'png:-'], startupinfo=startupinfo)
    else:
        raise ValueError('unknown converter {} or incompatible image format'.format(converter))
    return png

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
        debug(ex)
    return width, height

# @see https://en.wikipedia.org/wiki/Data_URI_scheme#Syntax
def parse_data_uri(uri: str) -> tuple:
    if not uri.startswith('data:') or ',' not in uri:
        raise ValueError('invalid data uri')
    media_type, _, raw_data = uri[5:].partition(',')
    data = b64decode(raw_data) if media_type.endswith(';base64') else urllib.parse.unquote_to_bytes(raw_data)
    mime = media_type.split(';')[0] if media_type else 'text/plain'
    return mime, data

def local_path(view: sublime.View, url: str):
    if os.path.isabs(url):
        return url
    else:
        file_name = view.file_name()
        if not file_name:
            return None
        else:
            return os.path.abspath(os.path.join(os.path.dirname(file_name), url))

def image_preview(view: sublime.View, region: sublime.Region, settings: sublime.Settings, extensionless_image_preview: bool, on_pre_show_popup, on_hide_popup) -> None:
    url = view.substr(region)
    if view.match_selector(region.a, 'punctuation.definition.string.begin | punctuation.definition.link.begin'):
        url = url[1:]
    if view.match_selector(region.b - 1, 'punctuation.definition.string.end | punctuation.definition.link.end'):
        url = url[:-1]
    if url.startswith('data:'):
        sublime.set_timeout_async(lambda: data_uri_image_popup(view, region, url, on_pre_show_popup, on_hide_popup))
    else:
        image_format = format_from_url(url)
        if image_format == ImageFormat.SVG and settings.get('svg_converter') not in SUPPORTED_CONVERTERS[ImageFormat.SVG]:
            return
        elif image_format == ImageFormat.WEBP and settings.get('webp_converter') not in SUPPORTED_CONVERTERS[ImageFormat.WEBP]:
            return
        elif image_format == ImageFormat.AVIF and settings.get('avif_converter') not in SUPPORTED_CONVERTERS[ImageFormat.AVIF]:
            return
        if url.lower().startswith(SUPPORTED_PROTOCOLS):
            if image_format in NATIVE_IMAGE_FORMATS + CONVERTABLE_IMAGE_FORMATS or extensionless_image_preview:
                sublime.set_timeout_async(lambda: web_image_popup(view, region, url, on_pre_show_popup, on_hide_popup))
        elif image_format in NATIVE_IMAGE_FORMATS + CONVERTABLE_IMAGE_FORMATS:
            if url.startswith('file://'):
                url = url[7:]
            sublime.set_timeout_async(lambda: local_image_popup(view, region, url, on_pre_show_popup, on_hide_popup))

def data_uri_image_popup(view: sublime.View, region: sublime.Region, data_uri: str, on_pre_show_popup, on_hide_popup) -> None:
    try:
        mime, data = parse_data_uri(data_uri)
    except Exception as ex:
        debug(ex)
        return
    if mime == MimeType.SVG:
        converter = sublime.load_settings(SETTINGS_FILE).get('svg_converter')
        try:
            data = convert_bytes2png(data, ImageFormat.SVG, converter)
        except Exception as ex:
            debug(ex)
            return
        data_base64 = b64encode(data).decode('ascii')
        data_uri = data_uri_template.format(mime, data_base64)
    elif mime == MimeType.WEBP:
        converter = sublime.load_settings(SETTINGS_FILE).get('webp_converter')
        try:
            data = convert_bytes2png(data, ImageFormat.WEBP, converter)
        except Exception as ex:
            debug(ex)
            return
        data_base64 = b64encode(data).decode('ascii')
        data_uri = data_uri_template.format(mime, data_base64)
    elif mime == MimeType.AVIF:
        converter = sublime.load_settings(SETTINGS_FILE).get('avif_converter')
        try:
            data = convert_bytes2png(data, ImageFormat.AVIF, converter)
        except Exception as ex:
            debug(ex)
            return
        data_base64 = b64encode(data).decode('ascii')
        data_uri = data_uri_template.format(mime, data_base64)
    elif mime not in [MimeType.PNG, MimeType.JPEG, MimeType.GIF, MimeType.BMP]:
        return
    width, height = image_size(data)
    image_popup(view, region, width, height, data_uri, on_pre_show_popup, on_hide_popup)

def local_image_popup(view: sublime.View, region: sublime.Region, url: str, on_pre_show_popup, on_hide_popup) -> None:
    path = local_path(view, url)
    if not path or not os.path.isfile(path):
        return
    image_format = format_from_url(path)
    if image_format in CONVERTABLE_IMAGE_FORMATS:
        if image_format == ImageFormat.SVG:
            converter = sublime.load_settings(SETTINGS_FILE).get('svg_converter')
        elif image_format == ImageFormat.WEBP:
            converter = sublime.load_settings(SETTINGS_FILE).get('webp_converter')
        elif image_format == ImageFormat.AVIF:
            converter = sublime.load_settings(SETTINGS_FILE).get('avif_converter')
        else:
            return
        debug('loading image from', path)
        try:
            data = convert_file2png(path, image_format, converter)
        except Exception as ex:
            debug(ex)
            return
        width, height = image_size(data)
        data_base64 = b64encode(data).decode('ascii')
        src = data_uri_template.format(MimeType.PNG, data_base64)
    else:
        debug('loading image from', path)
        with open(path, 'rb') as data:
            width, height = image_size(data)
        src = 'file://' + path
    image_popup(view, region, width, height, src, on_pre_show_popup, on_hide_popup)

def web_image_popup(view: sublime.View, region: sublime.Region, url: str, on_pre_show_popup, on_hide_popup) -> None:
    mime, data = request_img(url)
    if not mime or not data:
        return
    image_format = MIME_TYPE_FORMAT_MAP[mime] if mime in MIME_TYPE_FORMAT_MAP else ImageFormat.UNSUPPORTED
    if image_format in CONVERTABLE_IMAGE_FORMATS:
        if image_format == ImageFormat.SVG:
            converter = sublime.load_settings(SETTINGS_FILE).get('svg_converter')
        elif image_format == ImageFormat.WEBP:
            converter = sublime.load_settings(SETTINGS_FILE).get('webp_converter')
        elif image_format == ImageFormat.AVIF:
            converter = sublime.load_settings(SETTINGS_FILE).get('avif_converter')
        else:
            return
        mime = MimeType.PNG
        try:
            data = convert_bytes2png(data, image_format, converter)
        except Exception as ex:
            debug(ex)
            return
    width, height = image_size(data)
    data_base64 = b64encode(data).decode('ascii')
    data_uri = data_uri_template.format(mime, data_base64)
    image_popup(view, region, width, height, data_uri, on_pre_show_popup, on_hide_popup)

def image_popup(view: sublime.View, region: sublime.Region, width: int, height: int, src: str, on_pre_show_popup, on_hide_popup) -> None:
    device_scale_factor = EM_SCALE_FACTOR * view.em_width()
    scaled_width, scaled_height = scale_image(width, height, device_scale_factor)
    popup_border_width = sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
    popup_width = scaled_width + int(2 * popup_border_width * device_scale_factor)
    label = image_size_label(width, height)
    location = popup_location(view, region, popup_width)
    img_preview = '<img src="{}" width="{}" height="{}" /><div class="img-size">{}</div>'.format(src, scaled_width, scaled_height, label)
    content = format_template(view, popup_width, img_preview)
    on_pre_show_popup(region)
    view.show_popup(content, POPUP_FLAGS, location, 1024, 1024, None, on_hide_popup)

def rgb_color_swatch(view: sublime.View, region: sublime.Region, on_pre_show_popup, on_hide_popup) -> None:
    popup_border_width = sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
    popup_width = int((40 + 2 * popup_border_width) * EM_SCALE_FACTOR * view.em_width())
    location = popup_location(view, region, popup_width)
    color_swatch = '<div class="color-swatch" style="background-color: {}"></div>'.format(view.substr(region))
    content = format_template(view, popup_width, color_swatch)
    on_pre_show_popup(region)
    view.show_popup(content, POPUP_FLAGS, location, 1024, 1024, None, on_hide_popup)

def rgba_color_swatch(view: sublime.View, region: sublime.Region, r: int, g: int, b: int, a: float, on_pre_show_popup, on_hide_popup) -> None:
    if a == 1.0:
        rgb_color_swatch(view, region, on_pre_show_popup, on_hide_popup)
        return
    _, _, lightness = hex2hsl(view.style()['background'])
    color_scheme_type = 'dark' if lightness < 0.5 else 'light'  # @see https://www.sublimetext.com/docs/minihtml.html#predefined_classes
    bg_white = BACKGROUND_WHITE_PIXEL[color_scheme_type] * (1 - a)
    bg_black = BACKGROUND_BLACK_PIXEL[color_scheme_type] * (1 - a)
    r1, g1, b1 = int(r * a + bg_white), int(g * a + bg_white), int(b * a + bg_white)
    r2, g2, b2 = int(r * a + bg_black), int(g * a + bg_black), int(b * a + bg_black)
    data_base64 = checkerboard_png(r1, g1, b1, r2, g2, b2)
    device_scale_factor = EM_SCALE_FACTOR * view.em_width()
    scaled_width = int(40 * device_scale_factor)
    popup_border_width = sublime.load_settings(SETTINGS_FILE).get('popup_border_width')
    popup_width = int((40 + 2 * popup_border_width) * device_scale_factor)
    location = popup_location(view, region, popup_width)
    color_swatch = '<img src="data:image/png;base64,{}" width="{}" height="{}" />'.format(data_base64, scaled_width, scaled_width)
    content = format_template(view, popup_width, color_swatch)
    on_pre_show_popup(region)
    view.show_popup(content, POPUP_FLAGS, location, 1024, 1024, None, on_hide_popup)


class QuickViewHoverListener(sublime_plugin.EventListener):
    active_region = None

    def on_hover(self, view: sublime.View, point: int, hover_zone: int) -> None:
        if hover_zone != sublime.HOVER_TEXT:
            return
        if self.active_region and self.active_region.contains(point):  # prevent flickering on small mouse movements
            return
        settings = sublime.load_settings(SETTINGS_FILE)
        if view.match_selector(point, settings.get('image_scope_selector')):
            if not settings.get('image_preview'):
                return
            region = view.extract_scope(point)
            image_preview(view, region, settings, settings.get('extensionless_image_preview'), self.set_active_region, self.reset_active_region)
        elif settings.get('color_preview'):
            if view.match_selector(point, 'support.constant.color - support.constant.color.w3c.special - support.constant.color.w3c-special-color-keyword | constant.other.color.rgb-value'):
                region = view.extract_scope(point)
                if view.substr(region.b - 1) == ';':
                    region.b -= 1
                rgb_color_swatch(view, region, self.set_active_region, self.reset_active_region)
            elif view.match_selector(point, 'constant.other.color.rgba-value'):
                region = view.extract_scope(point)
                r, g, b, a = hex2rgba(view.substr(region))
                rgba_color_swatch(view, region, r, g, b, a, self.set_active_region, self.reset_active_region)
            elif view.match_selector(point, 'support.function.color | meta.property-value meta.function-call meta.group | meta.color meta.function-call meta.group'):
                line = view.line(point)
                text = view.substr(line)
                # @see https://facelessuser.github.io/coloraide/color/#color-matching
                for m in COLOR_FUNCTION_PATTERN.finditer(text):
                    if m.start() <= point - line.a <= m.end():
                        mcolor = Color.match(text, start=m.start())
                        if mcolor is not None:
                            region = sublime.Region(line.a + mcolor.start, line.a + mcolor.end)  # type: ignore
                            mcolor.color.convert('srgb', in_place=True)  # type: ignore
                            r = int(255 * mcolor.color.red)
                            g = int(255 * mcolor.color.green)
                            b = int(255 * mcolor.color.blue)
                            a = mcolor.color.alpha
                            rgba_color_swatch(view, region, r, g, b, a, self.set_active_region, self.reset_active_region)
                        return

    def set_active_region(self, region: sublime.Region) -> None:
        self.active_region = region

    def reset_active_region(self) -> None:
        self.active_region = None


class QuickViewCommand(sublime_plugin.TextCommand):
    popup_active = False

    def run(self, edit: sublime.Edit) -> None:
        if self.popup_active:
            self.view.hide_popup()
            return
        selections = self.view.sel()
        if not selections:
            debug('no selections in the active view')
            return
        region = selections[0]  # in case of multiple cursors only the first one is used, because there can only be a single popup be visible at a time
        is_empty_selection = region.empty()
        if is_empty_selection:
            point = region.b
            region = self.view.line(point)
        elif len(self.view.lines(region)) > 1:
            self.view.window().status_message('QuickView not possible for selections that span multiple lines')
            return
        else:
            point = region.begin()
        settings = sublime.load_settings(SETTINGS_FILE)
        if is_empty_selection and self.view.match_selector(point, settings.get('image_scope_selector')):
            region = self.view.extract_scope(point)
            image_preview(self.view, region, settings, True, self.set_popup_active, self.set_popup_inactive)  # @todo should show a message in the status bar if not successful
            return
        else:
            text = self.view.substr(region)
            offset = region.begin()
            for m in IMAGE_LINK_PATTERN.finditer(text):
                if not is_empty_selection or m.start() <= point - offset <= m.end():
                    link_region = sublime.Region(offset + m.start(), offset + m.end())
                    debug('potential image link', self.view.substr(link_region))
                    image_preview(self.view, link_region, settings, True, self.set_popup_active, self.set_popup_inactive)  # @todo should show a message in the status bar if not successful
                    return
                else:
                    break
            for m in COLOR_START_PATTERN.finditer(text):
                if not is_empty_selection or m.start() <= point - offset:
                    mcolor = Color.match(text, start=m.start())
                    if mcolor is not None and (not is_empty_selection or point - offset <= mcolor.end):  # type: ignore
                        color_region = sublime.Region(offset + mcolor.start, offset + mcolor.end)  # type: ignore
                        mcolor.color.convert('srgb', in_place=True)  # type: ignore
                        r = int(255 * mcolor.color.red)
                        g = int(255 * mcolor.color.green)
                        b = int(255 * mcolor.color.blue)
                        a = mcolor.color.alpha
                        rgba_color_swatch(self.view, color_region, r, g, b, a, self.set_popup_active, self.set_popup_inactive)
                        return
                else:
                    break
        msg = 'QuickView not possible at current cursor position' if is_empty_selection else 'QuickView not available for selection "{}"'.format(text)
        self.view.window().status_message(msg)

    def set_popup_active(self, region: sublime.Region) -> None:
        self.popup_active = True

    def set_popup_inactive(self) -> None:
        self.popup_active = False

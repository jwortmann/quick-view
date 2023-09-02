QuickView Changelog
===================

v1.4.5 (2023-09-02)
-------------------

  * Use native support for WebP images on Sublime Text built 4151 or newer,
    instead of requiring an external converter for WebP image previews.


v1.4.4 (2023-05-18)
-------------------

  * Fixed image preview not working with URLs from some websites.


v1.4.3 (2022-07-29)
-------------------

  * Fixed compatibility with coloraide 1.0 dependency.


v1.4.2 (2021-07-14)
-------------------

  * Fixed a missing comma in settings schema.

  * Added changelog file.


v1.4.1 (2021-07-07)
-------------------

  * Fixed color previews not working for CSS custom properties (variables) on
    Sublime Text 4 (needs to be invoked manually unless the default
    `"show_definitions"` popup is disabled).

  * If you hold the <kbd>Ctlr</kbd> (<kbd>Cmd</kbd> on macOS) key while clicking
    the "Open Image in new Tab" button in image previews, the tab will open to
    the right via the new tab multi-select feature on Sublime Text 4.

  * Slightly tweaked the popup background color for dark color schemes, so that
    it is almost indistinguishable from the popup shadow.


v1.4.0 (2021-06-19)
-------------------

  * Added a small icon for image previews which can be clicked to open an image
    in a new tab. This can be disabled in the settings by removing the
    `"open_image_button"` value from the `"popup_style"` option.

  * A small tweak for the label top border for image previews on ST4.


v1.3.0 (2021-05-27)
-------------------

  * Added a setting to allow substitutions for path aliases in image URLs. See
    the settings description for an example.

  * Enabled color previews for variables in Sass, SCSS and LESS. This will only
    work for very simple cases and for variables with standard CSS color syntax.
    Requires the Sass or the LESS package to work.

  * If SVG, WebP or AVIF images are converted with ImageMagick, transparent
    background in the images is replaced by a checkerboard pattern now. This can
    currently be disabled via an undocumented setting
    `"image_background_pattern": false`, but this option might be removed in the
    future.

  * Fixed color previews for hex-colors in the SCSS package, but I'd recommend
    to switch to the Sass package instead which seems to provide a better syntax
    for SCSS.


v1.2.0 (2021-05-19)
-------------------

  * Added support for color previews for CSS custom properties (variables). This
    will only work if there is a unique definition for the custom property in
    the file.

  * Added support for color previews for variables in Sublime color schemes if
    the PackageDev syntax is used. Overrides from user customizations for a
    color variable are taken into account.

  * Added settings schema for use with LSP-json.

  * A few small fixes and tweaks for color previews.


v1.1.0 (2021-04-26)
-------------------

  * Added a `quick_view` command, so that preview popups can be triggered for
    the current selection or cursor position via the command palette or a key
    binding.

  * Added a setting for popup style configuration.


v1.0.0 (2021-03-22)
-------------------

  * Initial release.

Iskoola Pota is a proprietary font (licensed to Microsoft for Sinhala Windows
builds), so it can't be redistributed here. The CSS in site.css is already
wired to load it from this folder:

  /static/fonts/IskoolaPota.woff2
  /static/fonts/IskoolaPota.ttf

To make Sinhala text on the site render in Iskoola Pota on every device
(not just ones that already have it installed, like some Windows/Sri Lankan
Android builds), drop your own licensed copy of the font here using exactly
these two filenames:

  app/static/fonts/IskoolaPota.woff2
  app/static/fonts/IskoolaPota.ttf

(A .ttf-to-woff2 converter, e.g. https://cloudconvert.com/ttf-to-woff2, works
fine if you only have the .ttf.)

Until a file is added, the site falls back to Noto Sans Sinhala for :lang(si)
and .sinhala-copy text, so nothing breaks — it just won't match Iskoola
Pota's exact letterforms.

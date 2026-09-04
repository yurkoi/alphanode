"""The one place the product version lives. Read by buildinfo (runtime), the build-stamp
generator (packaging/make_build_stamp.py), and mirrored by hand into AlphaNode.iss's
MyAppVersion — keep those two in step on a release bump."""
__version__ = '3.1.5'

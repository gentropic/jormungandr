# Freeze the jormungandr supervisor into the firmware image.
#
# Frozen modules execute in place from flash (XIP): their bytecode, string/bytes literals
# and qstrs live in the firmware, NOT the GC heap — unlike loading .mpy from the
# filesystem, which copies all of that into RAM at import. On a WROOM this is the
# difference between "barely fits" and "roomy" (see spec_inbox/ROADMAP notes).
#
# We include the port's default frozen set first (asyncio, the networking bundle,
# aioespnow, neopixel — all imported by the supervisor), then freeze the jorm package and
# the vendored microdot. JORM_SRC (the repo's supervisor/ dir) is exported by build.sh.
import os

include("$(PORT_DIR)/boards/manifest.py")

# JORM_SRC (the repo's supervisor/) is set by build.sh. Fall back to this manifest's own
# location (<repo>/firmware/manifest.py -> ../supervisor) so `idf.py qemu`, which re-evals
# the manifest without build.sh's env, still resolves it.
_jorm = os.environ.get("JORM_SRC")
if not _jorm:
    try:
        _jorm = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "supervisor")
    except NameError:
        _jorm = "/mnt/c/Users/endar/Documents/GitHub/jormungandr/supervisor"
package("jorm", base_path=_jorm)
package("microdot", base_path=os.path.join(_jorm, "lib"))

#!/usr/bin/env python

import os
import sys
import wget
from private.myfoscam import URL, OUTDIR

# NOTE: not in repository, but under private subdir in project, we have:
# __init__.py  -- blank file
# myfoscam.py  -- python file that has 2 globals: URL, which has private info,  and OUTDIR

class RedirectStdStreams(object):
    def __init__(self, stdout=None, stderr=None):
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr

    def __enter__(self):
        self.old_stdout, self.old_stderr = sys.stdout, sys.stderr
        self.old_stdout.flush(); self.old_stderr.flush()
        sys.stdout, sys.stderr = self._stdout, self._stderr

    def __exit__(self, exc_type, exc_value, traceback):
        self._stdout.flush(); self._stderr.flush()
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr


def webcam_snap(label, dtm, out_dir=OUTDIR):
    devnull = open(os.devnull, 'w')
    dstr = dtm.strftime('%Y-%m-%d_%H_%M')
    out_file = os.path.join(out_dir, dstr + '_' + label + '.jpg')
    with RedirectStdStreams(stdout=devnull):
        # you'll never see stdout with devnull for stdout
        filename = wget.download(URL, out=out_file)
    
    
if __name__ == '__main__':

    import datetime    
    webcam_snap('noon', datetime.datetime.now())

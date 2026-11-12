# -*- coding: utf-8 -*-
import os, sys
os.environ["QT_QPA_PLATFORM"]="offscreen"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication
from app import config as cfgmod
from app.ui.controls_bar import ControlsBar
class D(QObject):
    sig_time=pyqtSignal(float); sig_duration=pyqtSignal(float); sig_buffered=pyqtSignal(float)
    sig_chapters=pyqtSignal(list); sig_pause=pyqtSignal(bool); sig_volume=pyqtSignal(int)
    sig_mute=pyqtSignal(bool); sig_speed=pyqtSignal(float); sig_info=pyqtSignal(dict); sig_idle=pyqtSignal(bool)
app=QApplication([])
cfg=cfgmod.Config(path=os.path.join(os.environ.get("TEMP","/tmp"),"probe-bar.json"))
bar=ControlsBar(D(), cfg)
bar.show(); app.processEvents()
print("bar minHint", bar.minimumSizeHint().width(), bar.minimumSizeHint().height())
print("bar hint", bar.sizeHint().width())
print("layout min", bar.layout().minimumSize().width())
btns=[w for w in bar.findChildren(type(bar.btn_play))]
print("buttons", len(btns), "sum", sum(b.width() for b in btns))
print("scrub hint", bar.scrub.minimumSizeHint().width(), bar.scrub.sizeHint().width())
print("time hint", bar.lbl_time.sizeHint().width(), "speed", bar.lbl_speed.sizeHint().width())
print("vol", bar.vol.width(), "margins", bar.layout().contentsMargins().left(), bar.layout().contentsMargins().right())
bar.grab().save(os.path.join(os.path.dirname(__file__),"out","probe-bar.png"))

# app.py
# -*- coding: utf-8 -*-
import os, json, hashlib
from typing import List, Tuple, Dict
from dataclasses import dataclass

import mss
from PIL import Image
from dotenv import load_dotenv
import requests

from PySide6 import QtWidgets, QtCore, QtGui
import html as _html

# ====== 基本設定 ======
POLL_MS = 300								# キャプチャ間隔(ms)
CONFIG_PATH = "config.json"					# 設定保存先
TARGET_LANG = "JA"							# DeepLの出力言語（日本語固定）
ALLOWED_SRC = {"EN", "ZH", "KO"}			# この言語だけ日本語に翻訳（その他は原文表示）

# ====== OCR（pytesseract） ======
try:
	from pytesseract import image_to_string
	_OCR_OK = True
except Exception:
	_OCR_OK = False

def ocr_lines(pil_img: Image.Image) -> List[str]:
	# グレースケール＋多言語（日本語・英語・中(簡/繁)・韓）
	if not _OCR_OK:
		return []
	gray = pil_img.convert("L")
	# Tesseractの言語パックがない言語は自動的に無視される
	text = image_to_string(gray, lang="jpn+eng+chi_sim+chi_tra+kor")
	lines = [ln.strip() for ln in text.splitlines()]
	return [ln for ln in lines if ln]

# ====== ユーティリティ ======
def sha1(s: str) -> str:
	return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()

# ====== 設定 ======
@dataclass
class AppConfig:
	capture_rect: Tuple[int, int, int, int] = (40, 830, 780, 1070)	# (l,t,r,b)

	@staticmethod
	def load(path: str) -> "AppConfig":
		if os.path.exists(path):
			with open(path, "r", encoding="utf-8") as f:
				j = json.load(f)
			return AppConfig(tuple(j.get("capture_rect", (40,830,780,1070))))
		return AppConfig()

	def save(self, path: str):
		with open(path, "w", encoding="utf-8") as f:
			json.dump({"capture_rect": list(self.capture_rect)}, f, ensure_ascii=False, indent=2)

# ====== DeepL 翻訳 ======
class DeepLTranslator:
	def __init__(self):
		load_dotenv()
		self.api_key = os.getenv("DEEPL_API_KEY", "")
		self.endpoint = os.getenv("DEEPL_ENDPOINT", "https://api-free.deepl.com/v2/translate")
		self.cache: Dict[str, Tuple[str, str]] = {}	# 原文 -> (検出言語, 表示テキスト)

	def translate_batch(self, lines: List[str]) -> List[str]:
		if not lines:
			return []
		if not self.api_key:
			# 未設定なら原文返し
			return lines

		# 送信は未キャッシュのみ
		to_send = []
		index_map = []
		for i, ln in enumerate(lines):
			if ln in self.cache:
				continue
			to_send.append(("text", ln))
			index_map.append(i)

		if to_send:
			to_send.append(("target_lang", TARGET_LANG))
			resp = requests.post(
				self.endpoint,
				data=to_send,
				headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
				timeout=12
			)
			resp.raise_for_status()
			j = resp.json()
			trans = j.get("translations", [])
			for idx, tr in enumerate(trans):
				orig_i = index_map[idx]
				src = (tr.get("detected_source_language") or "").upper()
				text_ja = tr.get("text", lines[orig_i])
				if src in ALLOWED_SRC:
					# 言語インジケータ付けて保存
					self.cache[lines[orig_i]] = (src, f"[{src}] {text_ja}")
				else:
					self.cache[lines[orig_i]] = (src, lines[orig_i])

		out = []
		for ln in lines:
			src, shown = self.cache.get(ln, ("", ln))
			out.append(shown)
		return out

# ====== スクショ式ピッカー（フルスクでもOK） ======
class ScreenshotPicker(QtWidgets.QDialog):
	rectSelected = QtCore.Signal(tuple)	# (l,t,r,b) virtual screen 座標

	def __init__(self):
		super().__init__()
		self.setWindowTitle("範囲選択（スクリーンショット上）")
		self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.FramelessWindowHint)
		self.setModal(True)

		self._sct = mss.mss()
		mon = self._sct.monitors[0]	# 仮想スクリーン
		raw = self._sct.grab(mon)
		img = Image.frombytes("RGB", raw.size, raw.rgb)

		# PIL -> QImage
		data = img.tobytes()
		qimg = QtGui.QImage(data, img.width, img.height, img.width * 3, QtGui.QImage.Format_RGB888)
		self._pix = QtGui.QPixmap.fromImage(qimg)

		self._label = QtWidgets.QLabel()
		self._label.setPixmap(self._pix)
		self._label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

		layout = QtWidgets.QVBoxLayout()
		layout.setContentsMargins(0,0,0,0)
		layout.addWidget(self._label)
		self.setLayout(layout)

		self._rubber = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Rectangle, self._label)
		self._origin = None

		self.setGeometry(mon["left"], mon["top"], mon["width"], mon["height"])
		self.showFullScreen()
		self._label.installEventFilter(self)

	def eventFilter(self, obj, ev):
		if obj is self._label:
			if ev.type() == QtCore.QEvent.MouseButtonPress:
				self._origin = ev.position().toPoint()
				self._rubber.setGeometry(QtCore.QRect(self._origin, QtCore.QSize()))
				self._rubber.show()
				return True
			elif ev.type() == QtCore.QEvent.MouseMove and self._origin:
				rect = QtCore.QRect(self._origin, ev.position().toPoint()).normalized()
				self._rubber.setGeometry(rect)
				return True
			elif ev.type() == QtCore.QEvent.MouseButtonRelease and self._origin:
				self._rubber.hide()
				end = ev.position().toPoint()
				rect = QtCore.QRect(self._origin, end).normalized()
				l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
				self.rectSelected.emit((l, t, r, b))
				self.accept()
				return True
		return super().eventFilter(obj, ev)

# ====== メインウィンドウ ======
class LogWindow(QtWidgets.QMainWindow):
	def __init__(self, cfg: AppConfig):
		super().__init__()
		self.setWindowTitle("ChatLiveTranslate (OW2 / DeepL)")
		self.resize(760, 520)

		self.view = QtWidgets.QTextEdit(self)
		self.view.setReadOnly(True)
		self.view.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
		self.setCentralWidget(self.view)

		# ツールバー
		tb = self.addToolBar("controls")
		self.btn_select = QtWidgets.QPushButton("範囲選択")
		self.btn_select.clicked.connect(self.select_area)
		tb.addWidget(self.btn_select)

		self.btn_pause = QtWidgets.QPushButton("⏸ 一時停止")
		self.btn_pause.clicked.connect(self.toggle_pause)
		tb.addWidget(self.btn_pause)

		self.btn_clear = QtWidgets.QPushButton("ログ消去")
		self.btn_clear.clicked.connect(lambda: self.view.clear())
		tb.addWidget(self.btn_clear)

		self.status = self.statusBar()
		self.paused = False
		self.cfg = cfg

		# ===== デザイン（ダークテーマ＋大きめフォント） =====
		font = QtGui.QFont("Meiryo", 16)
		self.view.setFont(font)
		self.view.setStyleSheet("""
			QTextEdit {
				background-color: #1e1e1e;
				color: #e0e0e0;
				border: none;
				padding: 10px;
			}
			QScrollBar:vertical {
				background: #2a2a2a;
				width: 12px;
			}
			QScrollBar::handle:vertical {
				background: #555;
				border-radius: 6px;
			}
		""")

	def append_lines(self, lines: List[str]):
		if not lines:
			return
		cur = self.view.textCursor()
		cur.movePosition(QtGui.QTextCursor.End)
		for line in lines:
			color = "#e0e0e0"	# 既定：薄グレー
			if line.startswith("[EN]"):
				color = "#4fc3f7"	# 水色
			elif line.startswith("[ZH]"):
				color = "#ffca28"	# 黄
			elif line.startswith("[KO]"):
				color = "#81c784"	# 緑
			safe = _html.escape(line)
			cur.insertHtml(f'<span style="color:{color}">{safe}</span><br>')
		self.view.setTextCursor(cur)

	def toggle_pause(self):
		self.paused = not self.paused
		self.btn_pause.setText("▶ 再開" if self.paused else "⏸ 一時停止")
		self.status.showMessage("一時停止中" if self.paused else "翻訳中...", 1500)

	def select_area(self):
		picker = ScreenshotPicker()
		picker.rectSelected.connect(self._on_rect_selected)
		picker.exec()

	def _on_rect_selected(self, rect_tuple):
		l, t, r, b = rect_tuple
		self.cfg.capture_rect = (l, t, r, b)
		self.cfg.save(CONFIG_PATH)
		self.status.showMessage(f"選択: {self.cfg.capture_rect}", 3000)

# ====== 背景ワーカー ======
class Worker(QtCore.QObject):
	new_text = QtCore.Signal(list)

	def __init__(self, cfg: AppConfig, view: LogWindow):
		super().__init__()
		self.cfg = cfg
		self.view = view
		self._timer = QtCore.QTimer()
		self._timer.timeout.connect(self.tick)
		self._timer.start(POLL_MS)
		self._sct = mss.mss()
		self.translator = DeepLTranslator()
		self._seen_hashes = set()	# セッション中だけ保持

	def _grab_image(self) -> Image.Image:
		l, t, r, b = self.cfg.capture_rect
		mon = {"left": l, "top": t, "width": r - l, "height": b - t}
		raw = self._sct.grab(mon)
		return Image.frombytes("RGB", raw.size, raw.rgb)

	def tick(self):
		if self.view.paused:
			return
		try:
			img = self._grab_image()
			lines = ocr_lines(img)
			# 既出行（セッション中）はスキップ
			new_raw = []
			for ln in lines:
				h = sha1(ln)
				if h in self._seen_hashes:
					continue
				self._seen_hashes.add(h)
				new_raw.append(ln)

			if not new_raw:
				return

			out = self.translator.translate_batch(new_raw)
			if out:
				self.new_text.emit(out)

		except Exception as e:
			self.new_text.emit([f"(エラー) {e}"])

# ====== main ======
def main():
	cfg = AppConfig.load(CONFIG_PATH)
	app = QtWidgets.QApplication([])
	win = LogWindow(cfg)
	worker = Worker(cfg, win)
	worker.new_text.connect(lambda lines: (None if win.paused else win.append_lines(lines)))
	win.show()
	win.status.showMessage("準備完了。『範囲選択』でチャット欄を囲んでください。", 4000)
	app.exec()

if __name__ == "__main__":
	main()

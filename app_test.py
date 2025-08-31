# -*- coding: utf-8 -*-
"""
OW2 チャット自動翻訳ツール（解説入り）

このファイルは「どの実装が何をしていて、データがどう流れるか」を
初心者でも追いやすいように、コメントをとても多く入れています。

==== 全体の流れ（データフロー） =======================================
QTimer が一定間隔で tick() 
→ 画面を矩形キャプチャ 
→ OCRで行テキスト配列
→ セッション内で重複行を除去 
→ DeepLへまとめて翻訳（EN/ZH/KOのみ日本語化）
→ UI（QTextEdit）に色付きで追記
=======================================================================

依存ライブラリ（pip）：
	mss, pillow, PySide6, python-dotenv, requests, pytesseract

また、Tesseract OCR 本体（Windows版）＋ 言語データ（jpn/eng/chi_sim/chi_tra/kor）が必要。

Tips:
	- コメントは多いですが、実行に不要なものは一切ありません。
	- タブでインデントしています（ユーザさんの方針に合わせています）。
"""

# ===== 標準・外部ライブラリの import =====================================
import os
import json
import hashlib
import html as _html	# 表示時に安全のためテキストをエスケープ
from dataclasses import dataclass
from typing import List, Tuple

from dotenv import load_dotenv
import requests

# 画像キャプチャと画像処理
import mss
from PIL import Image

# GUI（Qt / PySide6）
from PySide6 import QtWidgets, QtCore, QtGui

# OCR（pytesseract）
try:
	from pytesseract import image_to_string
	_OCR_OK = True
except Exception:
	_OCR_OK = False


# ===== 定数（設定値はここで一元管理すると分かりやすい） ==================
POLL_MS = 250			# キャプチャ→OCR→翻訳 の周期（ミリ秒）。CPUが重ければ 350〜500 に
TARGET_LANG = "JA"		# DeepL の出力言語は日本語固定
ALLOWED_SRC = {"EN", "ZH", "KO"}	# DeepLが検出したときに翻訳対象とする言語
CONFIG_PATH = "config.json"	# 範囲（矩形）などを保存する設定ファイル


# ===== ユーティリティ ======================================================
def sha1(s: str) -> str:
	"""重複判定用のハッシュ関数（セッション内で既出行をスキップする）"""
	return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


# ===== 設定の保存/復元（AppConfig） =======================================
@dataclass
class AppConfig:
	"""アプリの基本設定を保持するクラス。
	現在はキャプチャ矩形だけですが、将来はここに項目を増やします。"""
	capture_rect: Tuple[int, int, int, int] = (40, 830, 780, 1070)

	@staticmethod
	def load(path: str) -> "AppConfig":
		"""JSON から設定を読み込む。ファイルがなければ既定値で作成。"""
		if os.path.exists(path):
			with open(path, "r", encoding="utf-8") as f:
				j = json.load(f)
			return AppConfig(tuple(j.get("capture_rect", (40, 830, 780, 1070))))
		return AppConfig()

	def save(self, path: str):
		"""設定を JSON に書き出す。範囲選択のたびに呼ばれる。"""
		with open(path, "w", encoding="utf-8") as f:
			json.dump({"capture_rect": list(self.capture_rect)}, f, ensure_ascii=False, indent=2)


# ===== DeepL 翻訳（サービス層） ===========================================
class DeepLTranslator:
	"""DeepL API を叩いて翻訳するクラス。
	- .env から API キー と エンドポイントを読み込み
	- 行ごとにポスト（順序はレスポンス配列順で保持）
	- DeepL の検出言語が EN/ZH/KO のときだけ日本語訳を使い、先頭に [EN]/[ZH]/[KO] を付ける
	- それ以外の言語は原文のまま返す
	"""

	def __init__(self):
		load_dotenv()
		self.api_key = os.getenv("DEEPL_API_KEY")
		# Free と Pro でエンドポイントが違う。Free を既定にし、.env で上書き可能にする。
		self.endpoint = os.getenv("DEEPL_ENDPOINT", "https://api-free.deepl.com/v2/translate")

	def translate_batch(self, lines: List[str]) -> List[str]:
		if not self.api_key or not lines:
			# APIキーなし → そのまま返す（開発・デバッグ時に便利）
			return lines

		# DeepL へフォームエンコードで送る（順序を保ちたいので 1行=1項目）
		payload = [("text", ln) for ln in lines]
		payload.append(("target_lang", TARGET_LANG))

		resp = requests.post(
			self.endpoint,
			data=payload,
			headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
			timeout=10,
		)
		resp.raise_for_status()
		j = resp.json()

		out: List[str] = []
		for i, tr in enumerate(j.get("translations", [])):
			detected = tr.get("detected_source_language", "").upper()
			text_ja = tr.get("text", lines[i])
			if detected in ALLOWED_SRC:
				# 検出言語を頭に付けてわかりやすく
				out.append(f"[{detected}] {text_ja}")
			else:
				# 対象外言語は原文（例: 日本語など）
				out.append(lines[i])
		return out


# ===== OCR（画像 → 行テキスト配列） =====================================
def ocr_lines(pil_img: Image.Image) -> List[str]:
	"""矩形キャプチャした PIL 画像から、行ごとのテキスト配列を作る。
	- グレースケール化でOCR精度が安定
	- 言語は jpn+eng を基本に、必要なら chi_sim/chi_tra/kor を追加
	"""
	if not _OCR_OK:
		return []
	gray = pil_img.convert("L")
	# 日本語・英語をまず優先。中国語/韓国語を加える場合は lang を拡張
	text = image_to_string(gray, lang="jpn+eng")
	# 改行で分割し、空行は捨てる
	lines = [ln.strip() for ln in text.splitlines()]
	return [ln for ln in lines if ln]


# ===== 範囲選択の UI（スクショ式ピッカー） ================================
class ScreenshotPicker(QtWidgets.QDialog):
	"""実画面の上から直接ドラッグするのではなく、
	一度スクリーンショットを撮って、その画像上で矩形を選ぶ方式。
	→ フルスクリーン専有のゲームでも確実に動作する。
	"""

	rectSelected = QtCore.Signal(tuple)	# (left, top, right, bottom) を返す

	def __init__(self):
		super().__init__()
		self.setWindowTitle("範囲選択（スクリーンショット上）")
		self.setWindowFlags(
			QtCore.Qt.WindowStaysOnTopHint |
			QtCore.Qt.FramelessWindowHint
		)
		self.setModal(True)

		# 仮想スクリーン全体のスクリーンショットを取得
		self._sct = mss.mss()
		mon = self._sct.monitors[0]
		raw = self._sct.grab(mon)
		img = Image.frombytes("RGB", raw.size, raw.rgb)

		# PIL → QImage → QPixmap（Qtで表示するための変換）
		data = img.tobytes()
		qimg = QtGui.QImage(data, img.width, img.height, img.width * 3, QtGui.QImage.Format_RGB888)
		self._pix = QtGui.QPixmap.fromImage(qimg)

		# 画像を貼る QLabel を用意
		self._label = QtWidgets.QLabel()
		self._label.setPixmap(self._pix)
		self._label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

		# レイアウトにラベルを敷き詰め
		layout = QtWidgets.QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.addWidget(self._label)
		self.setLayout(layout)

		# 選択範囲の可視化（RubberBand）
		self._rubber = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Rectangle, self._label)
		self._origin = None

		# ダイアログを仮想スクリーン原点に合わせて全画面表示
		self.setGeometry(mon["left"], mon["top"], mon["width"], mon["height"])
		self.showFullScreen()
		self._label.installEventFilter(self)

	def eventFilter(self, obj, ev):
		"""ラベル上のマウスイベントを拾って、ドラッグ矩形を決める。"""
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


# ===== メインウィンドウ（UI層） ============================================
class LogWindow(QtWidgets.QMainWindow):
	"""訳文ログの表示・操作ボタンのあるメインウィンドウ。"""
	def __init__(self, cfg: AppConfig):
		super().__init__()
		self.setWindowTitle("ChatLiveTranslate (OW2 / DeepL)")
		self.resize(820, 520)

		# 中央のテキストビュー
		self.view = QtWidgets.QTextEdit(self)
		self.view.setReadOnly(True)
		self.view.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
		self.setCentralWidget(self.view)

		# ツールバー（範囲選択／一時停止／ログ消去）
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

		# ===== 見た目（テーマ/フォント） =====
		font = QtGui.QFont("Meiryo", 16)
		self.view.setFont(font)
		self.view.setStyleSheet("""
			QTextEdit {
				background-color: #1e1e1e;
				color: #e0e0e0;
				border: none;
				padding: 10px;
			}
			QScrollBar:vertical { background: #2a2a2a; width: 12px; }
			QScrollBar::handle:vertical { background: #555; border-radius: 6px; }
		""")

	def append_lines(self, lines: List[str]):
		"""UI へ行を追加。
		[EN]/[ZH]/[KO] を先頭に持つ行は色分けして見やすくする。"""
		if not lines:
			return
		cur = self.view.textCursor()
		cur.movePosition(QtGui.QTextCursor.End)
		for line in lines:
			color = "#e0e0e0"
			if line.startswith("[EN]"):
				color = "#4fc3f7"
			elif line.startswith("[ZH]"):
				color = "#ffca28"
			elif line.startswith("[KO]"):
				color = "#81c784"
			safe = _html.escape(line)
			cur.insertHtml(f'<span style="color:{color}">{safe}</span><br>')
		self.view.setTextCursor(cur)

	def toggle_pause(self):
		"""OCR/翻訳ループを一時停止/再開。"""
		self.paused = not self.paused
		self.btn_pause.setText("▶ 再開" if self.paused else "⏸ 一時停止")
		self.status.showMessage("一時停止中" if self.paused else "翻訳中...", 1500)

	def select_area(self):
		"""スクショ式ピッカーを開いて、選んだ矩形を保存。"""
		picker = ScreenshotPicker()
		picker.rectSelected.connect(self._on_rect_selected)
		picker.exec()

	def _on_rect_selected(self, rect_tuple):
		l, t, r, b = rect_tuple
		self.cfg.capture_rect = (l, t, r, b)
		self.cfg.save(CONFIG_PATH)
		self.status.showMessage(f"選択: {self.cfg.capture_rect}", 2000)


# ===== ワーカー（心臓部：キャプチャ→OCR→翻訳→UI） ========================
class Worker(QtCore.QObject):
	"""一定間隔で処理を回すコンポーネント。UIとはシグナルで疎結合に。"""
	new_text = QtCore.Signal(list)	# 訳文の配列を UI に流す

	def __init__(self, cfg: AppConfig, view: LogWindow):
		super().__init__()
		self.cfg = cfg
		self.view = view
		self._timer = QtCore.QTimer()
		self._timer.timeout.connect(self.tick)
		self._timer.start(POLL_MS)
		self._sct = mss.mss()
		self.translator = DeepLTranslator()
		self._seen_hashes = set()  # セッション中のみ保持。再起動するとリセット。

	def _grab_image(self) -> Image.Image:
		"""指定矩形をキャプチャして PIL 画像に変換。"""
		l, t, r, b = self.cfg.capture_rect
		mon = {"left": l, "top": t, "width": r - l, "height": b - t}
		raw = self._sct.grab(mon)
		return Image.frombytes("RGB", raw.size, raw.rgb)

	def tick(self):
		"""1サイクル分の処理（キャプチャ→OCR→差分→翻訳→UI）。"""
		if self.view.paused:
			return
		try:
			# 1) 画面キャプチャ
			img = self._grab_image()
			# 2) OCR → 行配列
			lines = ocr_lines(img)
			# 3) セッション内の重複スキップ
			new_raw = []
			for ln in lines:
				h = sha1(ln)
				if h in self._seen_hashes:
					continue
				self._seen_hashes.add(h)
				new_raw.append(ln)

			if not new_raw:
				return  # 新しい行がないなら何もしない

			# 4) DeepL でまとめ翻訳（EN/ZH/KOのみ日本語化）
			out = self.translator.translate_batch(new_raw)
			if out:
				# 5) UI へ通知（Signal 経由でスレッド安全）
				self.new_text.emit(out)

		except Exception as e:
			# 例外は握りつぶさず、UIに流して利用者に見える形に
			self.new_text.emit([f"(エラー) {e}"])


# ===== エントリーポイント（main） ========================================
def main():
	"""アプリの初期化と起動。UIとワーカーを接続するだけ。"""
	cfg = AppConfig.load(CONFIG_PATH)

	app = QtWidgets.QApplication([])
	win = LogWindow(cfg)
	worker = Worker(cfg, win)

	# ワーカー → UI への接続
	worker.new_text.connect(lambda lines: (None if win.paused else win.append_lines(lines)))

	win.show()
	win.status.showMessage("準備完了。まずは『範囲選択』でチャット欄を囲んでください。")
	app.exec()


if __name__ == "__main__":
	main()

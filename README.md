# OW2 Chat Translator

> **Overwatch 2** のゲームチャットを  
> **画面キャプチャ → OCR → 翻訳 → オーバーレイ表示** まで行う Windows 向けツール

---

## Features
- ゲーム画面のチャットをリアルタイムでキャプチャ
- **Tesseract OCR** で文字認識
- **DeepL API** による翻訳
- 翻訳結果を **オーバーレイ表示**
- 設定ファイルでホットキーや言語を切替

---

## Tech Stack
- Python 3.11+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
- [DeepL API](https://www.deepl.com/)
- Pillow / PyAutoGUI / PySide6（UI 用）

---

## Requirements
- Windows 10/11
- Tesseract OCR がインストール済みでパスが通っていること
- DeepL API キー

---

## Setup

```bash
# 1) クローン
git clone https://github.com/skr-sp/ow2_ChatTranslator.git
cd ow2_ChatTranslator

# 2) 仮想環境 & 依存関係
python -m venv .venv
. .venv/Scripts/activate   # PowerShell の場合: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3) 環境変数
copy .env.example .env
# .env を開いて DEEPL_API_KEY=... を設定

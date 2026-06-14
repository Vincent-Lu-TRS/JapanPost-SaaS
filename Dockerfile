# ================================================================
# Hugging Face Spaces Docker 部署配置
# 用途：作為 Streamlit Community Cloud 的備選部署方案
# ================================================================

FROM python:3.11-slim

# 系統依賴（Playwright Chromium 需要）
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libnspr4 libasound2 \
    libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libxss1 libx11-xcb1 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安裝 Playwright Chromium
RUN playwright install chromium --with-deps

# 複製應用程式代碼
COPY . .

# HF Spaces 使用 port 7860
EXPOSE 7860

# 啟動 Streamlit
CMD ["streamlit", "run", "app.py", \
     "--server.port=7860", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableCORS=false"]

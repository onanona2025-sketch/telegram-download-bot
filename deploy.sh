#!/bin/bash
# ==============================================
#  نشر بوت التحميل على سيرفر لينكس
# ==============================================

set -e

echo "🔧 تحديث الحزم..."
apt update && apt upgrade -y

echo "🐍 تثبيت بايثون و FFmpeg..."
apt install -y python3 python3-pip python3-venv ffmpeg

echo "📁 إنشاء مجلد البوت..."
mkdir -p /root/bot
cd /root/bot

echo "📄 نسخ ملفات البوت..."
# انسخ ملفات البوت إلى هنا (bot.py, .env, requirements.txt)
# استخدم SCP أو FTP لنقل الملفات

echo "📦 تثبيت المتطلبات..."
pip3 install -r requirements.txt

echo "⚙️ إنشاء service..."
cp bot.service /etc/systemd/system/bot.service
systemctl daemon-reload
systemctl enable bot.service
systemctl start bot.service

echo ""
echo "✅ تم النشر بنجاح!"
echo ""
echo "لمتابعة السجلات:"
echo "  journalctl -u bot -f"
echo ""
echo "لإعادة التشغيل:"
echo "  systemctl restart bot"
echo ""
echo "لإيقاف البوت:"
echo "  systemctl stop bot"

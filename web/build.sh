#!/usr/bin/env bash
# 前端压缩构建：把 app.js / styles.css 压成 *.min.*，供 index.html 引用。
#
# 说明：源文件（app.js / styles.css）始终是唯一真相，压缩产物只是发布件。
#      运行时不需要 Node，也不需要构建工具；只有改完源码要出包时才跑这个脚本。
#      需要 Node.js（npx 会临时拉取 esbuild，无需全局安装）。
#
# 用法：  bash web/build.sh
set -euo pipefail
cd "$(dirname "$0")"

before_js=$(wc -c < app.js)
before_css=$(wc -c < styles.css)

npx --yes esbuild app.js --minify --target=es2017 --outfile=app.min.js
npx --yes esbuild styles.css --minify --outfile=styles.min.css

after_js=$(wc -c < app.min.js)
after_css=$(wc -c < styles.min.css)

echo "app.js     ${before_js} -> ${after_js}"
echo "styles.css ${before_css} -> ${after_css}"
echo "完成。记得同步更新 index.html 里的 ?v= 版本号，避免浏览器用旧缓存。"

/* 主题初始化：尽早执行，避免深浅色闪烁。
   独立文件是为了满足 Content-Security-Policy 的 script-src 'self'。 */
(function () {
  try {
    var theme = localStorage.getItem("aligo.theme");
    if (!theme) {
      theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.documentElement.dataset.theme = theme;
    // 同步 color-scheme：仅靠静态 meta 时，样式表生效前的首帧画布与
    // 滚动条会按系统偏好绘制，深色系统 + 浅色偏好会闪一帧深色
    document.documentElement.style.colorScheme = theme;
  } catch (error) {
    document.documentElement.dataset.theme = "light";
    document.documentElement.style.colorScheme = "light";
  }
})();

/* 主题初始化：尽早执行，避免深浅色闪烁。
   独立文件是为了满足 Content-Security-Policy 的 script-src 'self'。 */
(function () {
  try {
    var theme = localStorage.getItem("aligo.theme");
    if (!theme) {
      theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.documentElement.dataset.theme = theme;
  } catch (error) {
    document.documentElement.dataset.theme = "light";
  }
})();

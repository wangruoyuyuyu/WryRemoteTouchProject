# WryRemoteTouchProject
一个用于在TeamViewer等Windows远控软件中启用平板触摸的工具。<br>A tool to enable tablet PC touch in Windows remote controlling softwares like TeamViewer.

`main.py`（`server.exe`） 为服务器端，用于显示覆盖远控窗口的透明窗口，需要拖动窗口时可以按Ctrl+M切换透明度。<br>
`client.py` （`client.exe`）为客户端，用于接收触摸和鼠标信号并映射到系统输入，请通过命令 `client.exe 服务端IP` 启动该程序，随后用USBIP-Win软件连接localhost并挂载虚拟USB触摸设备。

## 注意

如果服务端和客户端不在同一局域网，请使用内网穿透或异地组网工具进行连接。

```main.py``` (```server.exe```) serves as the server-side component, responsible for displaying a transparent window that overlays the remote control interface. Press Ctrl+M to toggle the transparency when dragging the window.<br>
`client.py` (or `client.exe`) serves as the client, designed to receive touch and mouse signals and map them to system input. Launch the program via the command `client.exe [server IP]`, then connect to localhost using USBIP-Win software and mount the virtual USB touch device.

## Note
If the server and client are not on the same local area network, please use a NAT tunneling or remote networking tool for connection.

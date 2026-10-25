import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Bar item: "󰥔 3h12 acme". Left click opens the panel, right click starts/stops the timer.
BarWidget {
  id: root
  moduleName: "jd.uurtjie"

  property var status: ({})
  readonly property string uurtjieBin: Quickshell.env("HOME") + "/.local/bin/uurtjie"

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }

  function refresh() { if (!statusProc.running) statusProc.running = true }

  // Fire-and-forget CLI call, then refresh the bar shortly after.
  function run(args) {
    Quickshell.execDetached([root.uurtjieBin].concat(args))
    refreshSoon.restart()
  }

  // Same flow as the keybind: start, or confirm-stop + description popup.
  function timer() {
    Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/uurtjie-timer"])
    refreshSoon.restart()
  }

  // Live daemon log in a terminal (Ctrl-C closes it).
  function logs() {
    Quickshell.execDetached(["xdg-terminal-exec", "journalctl", "--user", "-u", "uurtjie", "-f", "-n", "100"])
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()

  Process {
    id: statusProc
    command: [root.uurtjieBin, "status", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.status = JSON.parse(this.text) } catch (e) { root.status = ({ bar_text: "󰚌 uurtjie?" }) }
      }
    }
  }

  Timer {
    interval: 10000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer { id: refreshSoon; interval: 600; onTriggered: root.refresh() }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.status.bar_text || "󰥔 –"
    tooltipText: (root.status.tooltip || "Uurtjie") + (root.status.version ? "\nv" + root.status.version : "")
    opacity: (root.status["class"] === "idle" || root.status["class"] === "stopped") ? 0.55 : 1.0
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) { root.refresh(); root.toggle() }
      else if (buttonCode === Qt.RightButton) root.timer()
      else if (buttonCode === Qt.MiddleButton) root.logs()
    }
  }
}

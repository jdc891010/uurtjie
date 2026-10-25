import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "jd.uurtjie"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var s: hostWidget ? hostWidget.status : ({})
  readonly property string widgetVersion: "0.4.1"   // keep in sync with manifest.json
  readonly property string family: root.bar ? root.bar.fontFamily : Style.font.family

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }
  function run(args) { if (root.hostWidget) root.hostWidget.run(args) }

  component Line: Text {
    width: parent ? parent.width : 0
    color: root.barForeground
    font.family: root.family
    font.pixelSize: Style.font.body
    wrapMode: Text.WordWrap
  }

  // Today's minutes worked per clock hour: x = hour, y = 0-60 minutes. Always spans 00h-23h. Hover a bar for its exact minutes.
  component HourChart: Item {
    id: chart
    property var minutes: []
    property int nowHour: -1
    property int hovered: -1
    readonly property real maximumMinutes: Math.max.apply(Math, [60].concat(minutes))

    readonly property var span: {
      return [0, 23]   // full day: long days are common
    }
    readonly property int count: span[1] - span[0] + 1
    readonly property real axisWidth: Style.space(22)
    readonly property real plotHeight: Style.space(64)
    readonly property real labelHeight: Style.space(14)
    readonly property real colWidth: plot.width / Math.max(1, count)

    function pad(h) { return (h < 10 ? "0" : "") + h }
    function minutesAt(h) { return h >= 0 && h < minutes.length ? minutes[h] : 0 }

    width: parent ? parent.width : 0
    implicitHeight: plotHeight + labelHeight + Style.space(4)

    // y axis: 60 / 30 / 0 minutes, with a faint gridline each
    Repeater {
      model: [chart.maximumMinutes, chart.maximumMinutes / 2, 0]
      delegate: Item {
        width: chart.width
        y: chart.plotHeight * (1 - modelData / chart.maximumMinutes) - lab.implicitHeight / 2
        Text {
          id: lab
          width: chart.axisWidth - Style.space(4)
          horizontalAlignment: Text.AlignRight
          text: modelData
          color: root.barForeground
          opacity: 0.6
          font.family: root.family
          font.pixelSize: Style.font.bodySmall
        }
        Rectangle {
          x: chart.axisWidth
          y: lab.implicitHeight / 2
          width: chart.width - chart.axisWidth
          height: 1
          color: root.barForeground
          opacity: 0.15
        }
      }
    }

    Item {
      id: plot
      x: chart.axisWidth
      width: chart.width - chart.axisWidth
      height: chart.plotHeight

      Repeater {
        model: chart.count
        delegate: Item {
          readonly property int hour: chart.span[0] + index
          readonly property real mins: chart.minutesAt(hour)
          readonly property real gap: Math.max(1, chart.colWidth * 0.14)
          x: index * chart.colWidth
          width: chart.colWidth
          height: chart.plotHeight

          Rectangle {      // 0-60 track, so a short bar reads as "a little of the hour"
            x: parent.gap
            width: parent.width - parent.gap * 2
            height: parent.height
            color: root.barForeground
            opacity: parent.hour === chart.nowHour || parent.hour === chart.hovered ? 0.2 : 0.07
          }
          Rectangle {
            x: parent.gap
            width: parent.width - parent.gap * 2
            height: parent.mins > 0 ? Math.max(2, parent.height * parent.mins / chart.maximumMinutes) : 0
            anchors.bottom: parent.bottom
            color: root.barForeground
            opacity: parent.hour === chart.hovered ? 1.0 : 0.8
          }
          Text {
            y: chart.plotHeight + Style.space(2)
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            visible: chart.count <= 14 || parent.hour % 3 === 0
            text: parent.hour
            color: root.barForeground
            opacity: parent.hour === chart.nowHour ? 1.0 : 0.6
            font.family: root.family
            font.pixelSize: Style.font.bodySmall
          }
          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onEntered: chart.hovered = parent.hour
            onExited: if (chart.hovered === parent.hour) chart.hovered = -1
          }
        }
      }
    }
  }

  component Action: Rectangle {
    id: btn
    property string label
    signal clicked()
    implicitWidth: lbl.implicitWidth + Style.space(20)
    implicitHeight: lbl.implicitHeight + Style.space(10)
    radius: Style.space(6)
    color: "transparent"
    border.width: 1
    border.color: root.barForeground
    opacity: area.containsMouse ? 1.0 : 0.75
    Text {
      id: lbl
      anchors.centerIn: parent
      text: btn.label
      color: root.barForeground
      font.family: root.family
      font.pixelSize: Style.font.body
    }
    MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; onClicked: btn.clicked() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(300))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(8)

        Line {
          text: "Human time"
          font.pixelSize: Style.font.bodySmall
          opacity: 0.6
        }
        Line {
          text: "Today " + (root.s.today ? root.s.today.hm : "0h00")
          font.pixelSize: Style.font.displayLarge
        }
        Line {
          text: "Week " + (root.s.week ? root.s.week.hm : "–") + "   ·   Month " + (root.s.month ? root.s.month.hm : "–")
          opacity: 0.8
        }

        Line {
          visible: !!(root.s.earnings && root.s.earnings.month_text)
          text: root.s.earnings
            ? "Earned this month  " + root.s.earnings.month_text
              + (root.s.earnings.today_text ? "   (+" + root.s.earnings.today_text + " today)" : "")
            : ""
        }

        Repeater {
          model: root.s.today ? root.s.today.projects : []
          delegate: Line { text: "  " + modelData.name + "   " + modelData.hm }
        }
        Line {
          visible: !!(root.s.invoice_adjustments && root.s.invoice_adjustments.note)
          text: root.s.invoice_adjustments ? root.s.invoice_adjustments.note : ""
          font.pixelSize: Style.font.bodySmall
          opacity: 0.6
        }

        Line {
          id: chartTitle
          visible: !!root.s.hourly
          text: chart.hovered >= 0
            ? chart.pad(chart.hovered) + ":00–" + chart.pad(chart.hovered + 1) + ":00   "
              + Math.round(chart.minutesAt(chart.hovered)) + " min"
            : "Human time by hour · minutes worked" + (chart.maximumMinutes > 60 ? " (repeated hour combined)" : "")
          opacity: chart.hovered >= 0 ? 1.0 : 0.6
          font.pixelSize: Style.font.bodySmall
        }
        HourChart {
          id: chart
          visible: !!root.s.hourly
          minutes: root.s.hourly ? root.s.hourly.minutes : []
          nowHour: root.s.hourly ? root.s.hourly.now_hour : -1
        }

        Line {
          visible: !!root.s.active
          text: root.s.active
            ? "● " + root.s.active.name + " · " + root.s.active.source + " · " + root.s.active.label
              + (root.s.active.agent ? " (" + root.s.active.agent + ")" : "")
            : ""
        }
        Line {
          visible: !root.s.active && !!root.s.last
          text: root.s.last ? "last: " + root.s.last.source + " · " + root.s.last.label + " · " + root.s.last.ago : ""
          opacity: 0.5
        }
        Line {
          visible: !!root.s.meeting
          text: root.s.meeting
            ? "󰍬 In a call · " + root.s.meeting.name + (root.s.meeting.app ? " · " + root.s.meeting.app : "")
              + " · " + root.s.meeting.minutes + " min"
            : ""
        }
        Line {
          visible: !!root.s.timer
          text: root.s.timer ? "󰔛 Timer: " + root.s.timer.project + " " + root.s.timer.hm : ""
        }
        Line {
          visible: root.s.running === false
          text: "uurtjie is not running — systemctl --user start uurtjie"
          opacity: 0.8
        }

        Line {
          visible: !!(root.s.invoices && root.s.invoices.text)
          text: root.s.invoices ? "Invoices: " + root.s.invoices.text : ""
          opacity: (root.s.invoices && root.s.invoices.overdue > 0) ? 1.0 : 0.6
        }

        Flow {
          width: parent.width
          spacing: Style.space(8)
          Action {
            label: root.s.timer ? "Stop timer" : "Start timer"
            onClicked: { if (root.hostWidget) root.hostWidget.timer(); root.close() }
          }
          Action {
            label: "Week in terminal"
            onClicked: Quickshell.execDetached(["sh", "-c", "xdg-terminal-exec sh -c 'uurtjie week; echo; read -n1 -p \"press a key\"'"])
          }
          Action {
            label: "Export"
            onClicked: Quickshell.execDetached(["sh", "-c",
              "out=$(\"$HOME/.local/bin/uurtjie\" export 2>&1); notify-send Uurtjie \"$out\""])
          }
          Action {
            label: "Live logs"
            onClicked: Quickshell.execDetached(["xdg-terminal-exec", "journalctl", "--user", "-u", "uurtjie", "-f", "-n", "100"])
          }
        }

        Line {
          readonly property bool stale: !!root.s.version && root.s.version !== root.widgetVersion
          text: stale
            ? "uurtjie v" + root.s.version + " · widget v" + root.widgetVersion + " — re-run install.sh to update the widget"
            : "uurtjie v" + (root.s.version || root.widgetVersion)
          opacity: stale ? 1.0 : 0.5
        }
      }
    }
  }
}

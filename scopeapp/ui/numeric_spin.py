"""明确的加减按钮，以及不会被采集刷新覆盖的数字编辑状态。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox, QToolButton


class NumericSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.editing = False
        self.setKeyboardTracking(False)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setStyleSheet("QDoubleSpinBox {padding-left:29px;padding-right:29px;}")
        self.setMinimumWidth(100)
        self.minus_button = QToolButton(self)
        self.plus_button = QToolButton(self)
        for button, label, step in ((self.minus_button, "−", -1), (self.plus_button, "+", 1)):
            button.setText(label)
            button.setFocusPolicy(Qt.NoFocus)
            button.setAutoRepeat(True)
            button.setToolTip("减小数值" if step < 0 else "增大数值")
            button.setStyleSheet("QToolButton {padding:0;border:0;border-radius:3px;background:#24364b;color:#dceaff;font-size:17px;} QToolButton:hover {background:#365477;} QToolButton:disabled {color:#596b7c;}")
            button.clicked.connect(lambda checked=False, direction=step: self.stepBy(direction))
        self.lineEdit().textEdited.connect(self._editing_started)
        self.editingFinished.connect(self._editing_finished)

    def _editing_started(self, *_):
        self.editing = True

    def _editing_finished(self):
        self.editing = False

    def stepBy(self, steps):
        self.interpretText()
        self.editing = False
        super().stepBy(steps)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.minus_button.setGeometry(2, 2, 24, max(1, self.height() - 4))
        self.plus_button.setGeometry(max(2, self.width() - 26), 2, 24, max(1, self.height() - 4))

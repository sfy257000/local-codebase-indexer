VERSION 5.00
Begin VB.Form frmChangeLine
   Caption         =   "Change line"
End
Attribute VB_Name = "frmChangeLine"

' 換線條碼解碼：掃描後拆出工單與產線代碼
Private Sub txtBarcode_KeyPress(KeyAscii As Integer)
    If KeyAscii = 13 Then
        Dim lineCode As String
        lineCode = Mid(txtBarcode.Text, 1, 4)
        Call ApplyLineChange(lineCode, Mid(txtBarcode.Text, 5))
    End If
End Sub

Private Sub ApplyLineChange(lineCode As String, workOrder As String)
    lblLine.Caption = lineCode
    lblOrder.Caption = workOrder
End Sub

CREATE PROCEDURE dbo.usp_Lot_UpdateStatus
    @LotNo nvarchar(20),
    @Status nvarchar(10)
AS
BEGIN
    UPDATE MES_Lot SET Status = @Status WHERE LotNo = @LotNo;
END
GO

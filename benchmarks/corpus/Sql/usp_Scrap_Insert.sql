-- 新增報廢資料
CREATE PROCEDURE dbo.usp_Scrap_Insert
    @LotNo nvarchar(20),
    @ReasonCode nvarchar(10),
    @Qty int,
    @OperatorId nvarchar(20)
AS
BEGIN
    INSERT INTO MES_ScrapRecord (LotNo, ReasonCode, Qty, OperatorId, Cancelled)
    VALUES (@LotNo, @ReasonCode, @Qty, @OperatorId, 0);
END
GO

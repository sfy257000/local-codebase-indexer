CREATE PROCEDURE dbo.usp_ApproveOrder @Id int
AS
BEGIN
    UPDATE dbo.Sales_OrderHeader SET Status = 'Approved' WHERE Id = @Id;
END;
GO

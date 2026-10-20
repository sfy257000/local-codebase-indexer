CREATE TABLE dbo.MES_Lot (
    LotNo nvarchar(20) NOT NULL PRIMARY KEY,
    PartNo nvarchar(20) NOT NULL,
    Layers int NOT NULL,
    Status nvarchar(10) NOT NULL,
    CreatedAt datetime NOT NULL DEFAULT GETDATE()
);
GO

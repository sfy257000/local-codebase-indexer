CREATE PROCEDURE dbo.usp_Inspection_Report
    @From datetime,
    @To datetime
AS
BEGIN
    SELECT l.LotNo, l.PartNo, i.Station, i.DefectCode
    FROM MES_Lot l
    JOIN MES_Inspection i ON i.LotNo = l.LotNo
    WHERE i.InspectedAt >= @From AND i.InspectedAt < @To;
END
GO

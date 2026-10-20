using System.Data;
using Dapper;
using Acme.Mes.Models;

namespace Acme.Mes.Repositories
{
    public class LotRepository
    {
        private readonly IDbConnection _conn;

        public LotRepository(IDbConnection conn)
        {
            _conn = conn;
        }

        public Lot Get(string lotNo)
        {
            return _conn.QueryFirstOrDefault<Lot>(
                "SELECT LotNo, PartNo, Layers, Status, CreatedAt FROM MES_Lot WHERE LotNo = @lotNo", new { lotNo });
        }

        public void Upsert(Lot lot)
        {
            _conn.Execute("MERGE MES_Lot AS t USING (SELECT @LotNo AS LotNo) s ON t.LotNo = s.LotNo " +
                          "WHEN MATCHED THEN UPDATE SET PartNo = @PartNo, Layers = @Layers " +
                          "WHEN NOT MATCHED THEN INSERT (LotNo, PartNo, Layers, Status) VALUES (@LotNo, @PartNo, @Layers, 'NEW');", lot);
        }

        public void UpdateStatus(string lotNo, string status)
        {
            _conn.Execute("usp_Lot_UpdateStatus", new { lotNo, status }, commandType: CommandType.StoredProcedure);
        }
    }
}

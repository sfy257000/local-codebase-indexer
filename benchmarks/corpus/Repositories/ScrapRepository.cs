using System.Collections.Generic;
using System.Data;
using Dapper;
using Acme.Mes.Models;

namespace Acme.Mes.Repositories
{
    public class ScrapRepository
    {
        private readonly IDbConnection _conn;

        public ScrapRepository(IDbConnection conn)
        {
            _conn = conn;
        }

        // 新增報廢資料，呼叫預存程序
        public void Insert(ScrapRecord r)
        {
            _conn.Execute("usp_Scrap_Insert",
                new { r.LotNo, r.ReasonCode, r.Qty, r.OperatorId },
                commandType: CommandType.StoredProcedure);
        }

        public void MarkCancelled(int scrapId)
        {
            _conn.Execute("UPDATE MES_ScrapRecord SET Cancelled = 1 WHERE Id = @scrapId", new { scrapId });
        }

        public IList<ScrapRecord> FindByLot(string lotNo)
        {
            return new List<ScrapRecord>(_conn.Query<ScrapRecord>(
                "SELECT Id, LotNo, ReasonCode, Qty, OperatorId FROM MES_ScrapRecord WHERE LotNo = @lotNo", new { lotNo }));
        }
    }
}

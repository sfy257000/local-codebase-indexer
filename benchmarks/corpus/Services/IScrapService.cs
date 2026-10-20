using System.Collections.Generic;
using Acme.Mes.Models;

namespace Acme.Mes.Services
{
    public interface IScrapService
    {
        void Save(ScrapRecord record);
        void Cancel(int scrapId);
        IList<ScrapRecord> GetByLot(string lotNo);
    }
}

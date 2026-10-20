using System;
using System.Collections.Generic;
using Acme.Mes.Models;
using Acme.Mes.Repositories;

namespace Acme.Mes.Services
{
    public class ScrapService : IScrapService
    {
        private readonly ScrapRepository _repo;

        public ScrapService(ScrapRepository repo)
        {
            _repo = repo;
        }

        public void Save(ScrapRecord record)
        {
            if (record.Qty <= 0)
                throw new ArgumentException("Qty must be positive");
            _repo.Insert(record);
        }

        public void Cancel(int scrapId)
        {
            _repo.MarkCancelled(scrapId);
        }

        public IList<ScrapRecord> GetByLot(string lotNo)
        {
            return _repo.FindByLot(lotNo);
        }
    }
}

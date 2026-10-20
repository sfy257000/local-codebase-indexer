using System;
using Acme.Mes.Models;
using Acme.Mes.Repositories;

namespace Acme.Mes.Services
{
    public class LotService
    {
        private readonly LotRepository _repo;

        public LotService(LotRepository repo)
        {
            _repo = repo;
        }

        public void Save(Lot lot)
        {
            _repo.Upsert(lot);
        }

        public void FinishProduction(string lotNo)
        {
            var lot = _repo.Get(lotNo);
            if (lot == null) throw new InvalidOperationException("unknown lot " + lotNo);
            _repo.UpdateStatus(lotNo, "FINISHED");
        }
    }
}

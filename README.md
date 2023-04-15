# dmut-resolvers

this repo generate resolvers.txt using github actions.

The task runs one time a day.

The process include:
- download a public list from https://public-dns.info/nameserver/us.txt
- check this list using [dnsfaster](https://github.com/bp0lr/dnsfaster)
- update this repo whit the result.


This file is generated to be used by [dmut](https://github.com/bp0lr/dmut)


### top20.txt

Top 20 public dns servers, in case you need something small and fast.
the error rate for this list is < 1%.

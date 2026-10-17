// generic_c_fail.c: returns 3. A run of it must end in a failure: it checks
// that a nonzero return from main reaches the finish register as a fail value
// (7).
int main(int argc, char **argv)
{
  return 3;
}

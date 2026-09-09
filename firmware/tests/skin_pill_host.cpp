#include "skin_pill.h"
#include "skin_sampler.h"
#include "sofia_aa_fonts.h"
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
#include <cmath>
#include <random>
#include <string>

uint64_t checks=0;
void check(bool ok,const char* message) { ++checks; if(!ok) { std::cerr<<message<<'\n'; std::exit(1); } }
struct Backend {
  int texts=0,rects=0,glows=0;
  int width(const char* text) {
    int result=0; for(;*text;++text) result+=SofiaBold24.glyphs[uint8_t(*text)-32].xAdvance;
    return result;
  }
  uint16_t blend(uint16_t bg,uint16_t ink,uint8_t alpha) {
    check(alpha<=15,"bad opacity");
    const auto channel=[&](int shift,int mask) { return ((((bg>>shift)&mask)*(15-alpha)+((ink>>shift)&mask)*alpha+7)/15); };
    return uint16_t(channel(11,31)<<11 | channel(5,63)<<5 | channel(0,31));
  }
  void glow(int x,int y,int w,int h,uint16_t,uint8_t) {
    ++glows; check(x>=30 && x+w+30<=368 && y+h+29<=98,"glow crosses protected region");
  }
  void rect(int x,int y,int w,int h,float r,uint16_t) {
    ++rects; check(x>=0 && y>=0 && x+w<=338 && y+h<=69 && r<=h/2.0f,"rect bounds");
  }
  void text(const char* s,int x,int baseline,uint16_t) {
    ++texts; check(x>=30 && x+width(s)<=338 && baseline>=24 && baseline<=66,"text bounds");
  }
  void center(const char* s,int x,int y,int w,int h,uint16_t c) {
    text(s,x+(w-width(s))/2,y+h/2+9,c);
  }
};
int main(int argc,char** argv) {
  check(argc==2,"compiled classic package required");
  std::ifstream input(argv[1],std::ios::binary);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)),{});
  check(!skin_format::validate(bytes.data(),bytes.size()),"classic rejected");
  const auto* h=reinterpret_cast<const skin_format::Header*>(bytes.data());
  check(h->schema==2,"not schema two");
  const auto* pills=skin_format::pills(h);
  Backend backend;
  const char* labels[]={"THINKING","NEED APPROVAL","DATA STALE","WAITING FOR SYNC"};
  const int advances[]={109,189,139,220};
  for(unsigned i=0;i<4;++i) {
    check(backend.width(labels[i])==advances[i] && backend.width(".")==6,"validation font metrics drift");
    check(pills[i].x==30 && pills[i].y==28 && pills[i].height==36,"original pill location/height changed");
    for(uint32_t ms=0;ms<4200;++ms) {
      skin_pill::draw(h,i,ms,backend);
      auto f=skin_pill::sample(bytes.data(),h->animations[i],ms);
      auto loop=skin_pill::sample(bytes.data(),h->animations[i],ms+1400);
      check(!memcmp(&f,&loop,sizeof(f)),"pill loop not periodic");
      if(i<2) {
        const double phase=double((ms%1400)/20*20)/1400.0;
        double wave=.5-.5*std::cos(6.283185307179586*phase);
        wave=wave*wave*(3-2*wave);
        double pulse=.3+.7*wave;
        check(f.values[0]==int(pulse*15+.5) && f.values[1]==f.values[0],"original body/text breathing differs");
        check(f.values[2]==int(64+166*std::pow(wave,1.4)+.5),"original halo differs");
        for(int dot=0;dot<3;++dot) {
          const double light=std::fmin(1.0,std::fmax(.3,pulse*(.45+.55*(.5+.5*std::sin(6.283185307179586*(phase*2-dot/3.0))))));
          check(f.values[3+dot]==int(light*15+.5),"original trailing dots differ");
        }
      } else check(f.values[0]==15 && f.values[2]==255,"old sync states must stay static");
      // Schema-two data must never be interpreted as a nine-cell image.
      auto unused=skin_sampler::sample(bytes.data(),h->animations[i],ms);
      check(!unused.reference && unused.cells[0]==0,"pill opacity was sampled as pixels");
    }
    skin_pill::draw(h,i,UINT32_MAX,backend);
  }
  for(size_t n=0;n<bytes.size();++n) check(skin_format::validate(bytes.data(),n)!=nullptr,"truncation accepted");
  std::mt19937 rng(0xC1A551C);
  for(int iteration=0;iteration<3000;++iteration) {
    auto changed=bytes;
    changed[rng()%changed.size()]=uint8_t(rng());
    if(skin_format::validate(changed.data(),changed.size())) continue;
    const auto* candidate=reinterpret_cast<const skin_format::Header*>(changed.data());
    for(unsigned i=0;i<4;++i) skin_pill::draw(candidate,i,UINT32_MAX,backend);
  }
  std::cout<<"Classic skin: "<<checks<<" checks passed; original geometry, pulse/dots, safe text/glow, schema rejection and periodicity.\n";
}
